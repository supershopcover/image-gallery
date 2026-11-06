"""Read-only image contact browser for a directory tree (Windows)."""
import json
import os
import queue
import subprocess
import sys
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageOps, ImageSequence, ImageTk

EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tif', '.tiff', '.avif'}
BG, PANEL, FG, MUTED = '#222529', '#303439', '#f1f2f4', '#bbc2c9'
THUMB = (190, 117)
ALL = '전체 폴더'
ANIMATION_BYTES = 600_000_000  # 움직이는 그림을 메모리에 펼칠 때의 상한 (넘으면 축소)
MAGIC = (b'\xff\xd8', b'\x89PNG', b'GIF8', b'RIFF', b'BM', b'II*\0', b'MM\0*')  # JPEG PNG GIF WebP BMP TIFF


def looks_like_image(path):
    """True when the first bytes are a known picture header (HTML saved as .jpg fails this)."""
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except OSError:
        return False
    return head.startswith(MAGIC) or head[4:8] == b'ftyp'  # ftyp: AVIF/HEIF


class Checks:
    """Per-folder memory of which files turned out not to be pictures: rel -> [mtime_ns, ok]."""
    def __init__(self):
        self.path = Path(os.environ.get('LOCALAPPDATA') or Path.home())/'이미지 모아보기'/'checked.json'
        try:
            with open(self.path, encoding='utf-8') as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def folder(self, root):
        return self.data.setdefault(str(root), {})

    def save(self):
        text = json.dumps(self.data, ensure_ascii=False)
        def write():
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix('.tmp')
                tmp.write_text(text, encoding='utf-8')
                tmp.replace(self.path)
            except OSError:
                pass
        threading.Thread(target=write, daemon=True).start()


@dataclass(frozen=True, slots=True)
class Entry:
    rel: str
    group: str
    modified: int
    name: str
    key: str


def scan(root, stop=None, known=None):
    """Return (entries, unreadable count, hidden count); files already known not to be pictures are left out."""
    entries = []
    skipped = hidden = 0
    known = known or {}
    dirs = [root]
    while dirs:
        folder = dirs.pop()
        try:
            with os.scandir(folder) as files:
                for f in files:
                    if stop is not None and stop.is_set():
                        return [], 0, 0
                    try:
                        if f.is_dir(follow_symlinks=False):
                            if not os.path.isjunction(f.path):
                                dirs.append(Path(f.path))
                        elif Path(f.name).suffix.lower() in EXTENSIONS and f.is_file(follow_symlinks=False):
                            rel = os.path.relpath(f.path, root)
                            modified = f.stat(follow_symlinks=False).st_mtime_ns
                            seen = known.get(rel)
                            if seen and seen[0] == modified and not seen[1]:
                                hidden += 1
                                continue
                            group = rel.split(os.sep, 1)[0] if os.sep in rel else '(최상위)'
                            entries.append(Entry(rel, group, modified, f.name, rel.casefold()))
                    except OSError:
                        skipped += 1
        except OSError:
            skipped += 1
    entries.sort(key=lambda e: (-e.modified, e.key))
    return entries, skipped, hidden


def load_picture(path, size):
    with Image.open(path) as image:
        dimensions = image.size
        image.draft('RGB', size)
        image.thumbnail(size, Image.Resampling.LANCZOS)
        shown = ImageOps.exif_transpose(image).convert('RGBA')
    return shown, dimensions


def path_search(text, root):
    text = text.strip().strip('"').strip().replace('₩', os.sep).replace('/', os.sep)
    if not os.path.isabs(text):
        return None, text.casefold().split()
    relative = Path(os.path.normpath(text))
    try:
        within = relative.relative_to(root)
    except ValueError:
        return False, []
    return within.as_posix().replace('/', os.sep).casefold(), []


def original_pixels(path):
    """Return [(frame, duration_ms), ...]; a still picture is a single frame."""
    with Image.open(path) as image:
        count = getattr(image, 'n_frames', 1)
        if count <= 1:
            return [(ImageOps.exif_transpose(image).convert('RGBA'), 0)]
        w, h = image.size
        shrink = min(1., (ANIMATION_BYTES / (w*h*4*count)) ** .5)
        size = (max(1, round(w*shrink)), max(1, round(h*shrink)))
        frames = []
        for frame in ImageSequence.Iterator(image):
            duration = frame.info.get('duration') or 0
            pixels = frame.convert('RGBA')
            if shrink < 1.:
                pixels = pixels.resize(size, Image.Resampling.BILINEAR)
            # 브라우저처럼 10ms 이하의 지연은 100ms로 본다.
            frames.append((pixels, duration if duration > 10 else 100))
        return frames


class ThumbnailRail(ttk.Frame):
    """Render only sidebar rows inside the viewport, sharing the gallery's workers."""
    row_height = 112

    def __init__(self, parent, gallery):
        super().__init__(parent)
        self.gallery = gallery
        self.jobs = {}
        self.cache = OrderedDict()
        self.images = {}
        self.items = {}
        self.scheduled = None
        self.canvas = tk.Canvas(self, width=156, bg=BG, highlightthickness=0, yscrollincrement=24)
        scroll = ttk.Scrollbar(self, command=self.canvas.yview)
        scroll.pack(side='right', fill='y')
        self.canvas.pack(fill='both', expand=True)
        def scroll_changed(first, last):
            scroll.set(first, last)
            self.schedule()
        self.canvas.configure(yscrollcommand=scroll_changed,
                              scrollregion=(0, 0, 156, len(gallery.filtered)*self.row_height))
        self.canvas.bind('<Configure>', lambda _: self.schedule())
        self.canvas.bind('<MouseWheel>', lambda e: self.canvas.yview_scroll(-int(e.delta/120)*3,'units'))
        self.canvas.bind('<Button-1>', self.click)

    def click(self, event):
        index = int(self.canvas.canvasy(event.y)//self.row_height)
        if 0 <= index < len(self.gallery.filtered):
            self.gallery.jump(index)
            self.gallery.viewer.focus_set()

    def follow(self, index):
        y = index*self.row_height
        top = self.canvas.canvasy(0)
        height = max(self.row_height,self.canvas.winfo_height())
        if y < top or y+self.row_height > top+height:
            target = y if y < top else y+self.row_height-height
            self.canvas.yview_moveto(max(0,target)/(self.row_height*len(self.gallery.filtered)))
        self.schedule()

    def schedule(self):
        if self.scheduled is None and self.winfo_ismapped():
            self.scheduled = self.after_idle(self.paint)

    def paint(self):
        self.scheduled = None
        if not self.winfo_ismapped():return
        canvas = self.canvas
        canvas.delete('all')
        self.images.clear()
        self.items.clear()
        first = max(0,int(canvas.canvasy(0)//self.row_height))
        last = min(len(self.gallery.filtered),int(canvas.canvasy(canvas.winfo_height())//self.row_height)+2)
        for i,job in list(self.jobs.items()):
            if not first <= i < last and job.cancel():
                del self.jobs[i]
        for i in range(first,last):
            entry = self.gallery.filtered[i]
            y = i*self.row_height
            selected = i==self.gallery.selection
            canvas.create_rectangle(3,y+2,152,y+self.row_height-2,
                                    outline='#83baf0' if selected else PANEL,
                                    fill='#354557' if selected else PANEL,width=2)
            self.items[i] = canvas.create_image(78,y+44)
            canvas.create_text(8,y+95,anchor='w',fill=FG,
                               text=f'{i+1}  {self.gallery.shorten(entry.name,15)}')
            if i in self.cache:
                self.cache.move_to_end(i)
                self.display(i,self.cache[i])
            elif i not in self.jobs:
                job = self.gallery.pool.submit(load_picture,self.gallery.root/entry.rel,(138,77))
                self.jobs[i] = job
                job.add_done_callback(lambda f,n=i: self.gallery.results.put(('rail',(self,n,f))))

    def display(self,index,image):
        if index not in self.items:return
        if image is None:
            self.canvas.create_text(78,index*self.row_height+44,text='미리보기 불가',fill=MUTED)
        else:
            photo=ImageTk.PhotoImage(image)
            self.images[index]=photo
            self.canvas.itemconfigure(self.items[index],image=photo)

    def finish(self,index,job):
        if self.jobs.get(index) is not job:return
        del self.jobs[index]
        try:image=job.result()[0]
        except Exception:image=None
        self.cache[index]=image
        if len(self.cache)>72:self.cache.popitem(last=False)
        self.display(index,image)

    def dispose(self):
        if self.scheduled is not None:self.after_cancel(self.scheduled)
        self.scheduled=None
        for job in self.jobs.values():job.cancel()
        self.jobs.clear()
        self.cache.clear()
        self.images.clear()

    def reset(self):
        """The list changed under us: forget cached rows (indices shifted) and repaint."""
        self.dispose()
        self.canvas.configure(scrollregion=(0,0,156,len(self.gallery.filtered)*self.row_height))
        self.schedule()


class InPlaceFullscreen:
    """Resize the existing Windows frame without remapping Tk's client area."""
    def __init__(self, window):
        import ctypes
        from ctypes import wintypes as wt

        class MonitorInfo(ctypes.Structure):
            _fields_ = [('size',wt.DWORD),('monitor',wt.RECT),('work',wt.RECT),('flags',wt.DWORD)]

        self.window = window
        self.saved = None
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        for name, args, result in [
            ('GetAncestor',[wt.HWND,wt.UINT],wt.HWND),
            ('GetWindowLongW',[wt.HWND,ctypes.c_int],wt.LONG),
            ('SetWindowLongW',[wt.HWND,ctypes.c_int,wt.LONG],wt.LONG),
            ('GetWindowRect',[wt.HWND,ctypes.POINTER(wt.RECT)],wt.BOOL),
            ('MonitorFromWindow',[wt.HWND,wt.DWORD],wt.HANDLE),
            ('GetMonitorInfoW',[wt.HANDLE,ctypes.POINTER(MonitorInfo)],wt.BOOL),
            ('SetWindowPos',[wt.HWND,wt.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wt.UINT],wt.BOOL),
        ]:
            api=getattr(self.user,name)
            api.argtypes,api.restype=args,result
        self.info=MonitorInfo
        self.ctypes=ctypes
        self.rect_type=wt.RECT

    def switch(self):
        c=self.ctypes
        hwnd=self.user.GetAncestor(self.window.winfo_id(),2)
        current=self.user.GetWindowLongW(hwnd,-16)
        entering=self.saved is None
        if entering:
            old=self.rect_type()
            info=self.info()
            info.size=c.sizeof(info)
            monitor=self.user.MonitorFromWindow(hwnd,2)
            if not self.user.GetWindowRect(hwnd,c.byref(old)) or not self.user.GetMonitorInfoW(monitor,c.byref(info)):
                raise c.WinError(c.get_last_error())
            target=info.monitor
            # WS_CAPTION | WS_THICKFRAME; keep the original maximize state intact.
            style=(current & ~0x00C40000) | 0x80000000
        else:
            style,target=self.saved
        self.user.SetWindowLongW(hwnd,-16,style)
        # SWP_FRAMECHANGED | SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOOWNERZORDER
        if not self.user.SetWindowPos(hwnd,None,target.left,target.top,
                                      target.right-target.left,target.bottom-target.top,0x0234):
            error=c.get_last_error()
            self.user.SetWindowLongW(hwnd,-16,current)
            raise c.WinError(error)
        self.saved=(current,old) if entering else None
        return entering


class Gallery:
    def __init__(self, window, root):
        self.window, self.root = window, root
        window.title('이미지 모아보기 · '+(root.name or str(root)))
        window.geometry('1160x780')
        window.minsize(640, 420)
        self.entries = []
        self.filtered = []
        self.cols = 1
        self.card_scale = 1.0
        self.empty_text = "표시할 이미지가 없습니다."
        self.source_image = None
        self.frames = []
        self.frame_at = 0
        self.animation_job = None
        self.decode_job = None
        self.results = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.tasks, self.visible, self.keep = {}, {}, {}
        self.cache = OrderedDict()
        self.selection = None
        self.preview = None
        self.rail_open = True
        self.fullscreen = False
        self.frame_mode = InPlaceFullscreen(window)
        self.return_view = None
        self.preview_job = None
        self.preview_id = 0
        self.scan_busy = False
        self.scan_stop = None
        self.closed = False
        self.after_filter = None
        self.after_paint = None
        self.checks = Checks()
        self.known = self.checks.folder(root)
        self.hidden = 0
        self.skipped = 0
        self.save_job = None
        self.anchor = None
        bar = ttk.Frame(window,padding=(12,10))
        bar.pack(fill='x')
        ttk.Label(bar,text='이미지 모아보기',font=('맑은 고딕',15,'bold')).pack(side='left',padx=(0,16))
        self.status = tk.StringVar(value='하위 폴더를 읽는 중…')
        ttk.Label(bar,textvariable=self.status).pack(side='left')
        self.refresh = ttk.Button(bar,text='새로고침',command=self.rescan)
        self.refresh.pack(side='right')
        ttk.Button(bar,text='폴더 열기…',command=self.choose_root).pack(side='right',padx=6)
        filters = ttk.Frame(window,padding=(12,0,12,10))
        filters.pack(fill='x')
        self.group = tk.StringVar(value=ALL)
        self.groups = ttk.Combobox(filters,state='readonly',textvariable=self.group,width=28)
        self.groups.pack(side='left')
        self.groups.bind('<<ComboboxSelected>>',lambda _:self.filter())
        self.dupes = tk.BooleanVar(value=False)
        self.dupe_toggle=ttk.Checkbutton(filters,text='중복 보관 폴더 포함',variable=self.dupes,command=self.filter)
        ttk.Label(filters,text='파일·경로 검색').pack(side='left',padx=(14,4))
        self.query=tk.StringVar()
        search=ttk.Entry(filters,textvariable=self.query)
        search.pack(side='left',fill='x',expand=True,padx=(0,10))
        self.query.trace_add('write',self.schedule_filter)
        frame=ttk.Frame(window);frame.pack(fill='both',expand=True)
        self.gallery_area=frame
        self.cv=tk.Canvas(frame,bg=BG,highlightthickness=0)
        self.sb=ttk.Scrollbar(frame,command=self.cv.yview)
        self.sb.pack(side='right',fill='y');self.cv.pack(fill='both',expand=True)
        self.cv.configure(yscrollcommand=self.scrolled)
        self.cv.bind('<Configure>',self.resize)
        self.cv.bind('<Control-MouseWheel>',self.scale_cards)
        self.cv.bind('<MouseWheel>',lambda e:self.cv.yview_scroll(-int(e.delta/120)*3,'units'))
        self.cv.bind('<Button-1>',self.click)
        self.cv.bind('<Double-Button-1>',lambda e:self.open_preview())
        self.cv.bind('<Return>',lambda _:self.open_preview())
        self.cv.configure(yscrollincrement=30)
        bottom=ttk.Frame(window,padding=(12,7));bottom.pack(fill='x')
        self.path_label=tk.StringVar(value='더블클릭: 사진 확대  ·  Ctrl+휠: 목록 크기')
        ttk.Button(bottom,text='원본 폴더 열기',command=self.reveal).pack(side='right')
        ttk.Label(bottom,textvariable=self.path_label,anchor='w').pack(side='left',fill='x',expand=True)
        self.top_bar,self.filters_bar,self.bottom_bar=bar,filters,bottom
        window.bind('<F5>',lambda _:self.rescan())
        window.bind('<Control-o>',lambda _:self.choose_root())
        window.protocol('WM_DELETE_WINDOW',self.close)
        self.rescan();window.after(40,self.poll)

    def choose_root(self):
        folder=filedialog.askdirectory(parent=self.window,title='이미지를 모아볼 폴더 선택',
                                       initialdir=str(self.root),mustexist=True)
        if folder:self.change_root(Path(folder).resolve())

    def change_root(self,folder):
        if self.scan_stop:self.scan_stop.set()
        if self.preview:self.close_preview()
        for job in self.tasks.values():job.cancel()
        self.tasks.clear();self.cache.clear()
        self.root=folder
        self.known=self.checks.folder(folder)
        self.window.title('이미지 모아보기 · '+(folder.name or str(folder)))
        self.entries=[];self.skipped=0;self.hidden=0
        self.group.set(ALL);self.query.set('');self.dupes.set(False)
        self.groups.configure(values=[ALL]);self.dupe_toggle.pack_forget()
        self.filter()
        self.scan_busy=False
        self.rescan(keep=False)

    def view_anchor(self):
        """What the user is looking at now, so a refresh can return to it."""
        top=None
        index=max(0,int(self.cv.canvasy(0))//self.cell_height)*self.cols
        if 0<=index<len(self.filtered):top=self.filtered[index].rel
        selected=self.filtered[self.selection] if self.selection is not None and 0<=self.selection<len(self.filtered) else None
        return {'top':top,'sel':selected.rel if selected else None,
                'modified':selected.modified if selected else None,'preview':bool(self.preview)}

    def rescan(self,keep=True):
        if self.scan_busy:return
        self.scan_busy=True
        self.anchor=self.view_anchor() if keep else None
        self.refresh.configure(state='disabled')
        self.status.set('하위 폴더를 읽는 중…')
        root=self.root;known=dict(self.known)
        self.scan_stop=stop=threading.Event()
        def work():
            try:self.results.put(('scan',(stop,scan(root,stop,known))))
            except Exception as exc:self.results.put(('scan_error',(stop,str(exc))))
        threading.Thread(target=work,daemon=True).start()

    def sweep(self):
        """Read the header of every file not yet checked; report fakes as they turn up."""
        stop=self.scan_stop;root=self.root
        todo=[(e.rel,e.modified) for e in self.entries if self.known.get(e.rel,[None])[0]!=e.modified]
        if not todo:return
        def work():
            batch=[]
            for rel,modified in todo:
                if stop.is_set():return
                ok=looks_like_image(root/rel)
                batch.append((rel,modified,ok))
                if not ok or len(batch)>=300:
                    self.results.put(('sweep',(stop,batch)));batch=[]
            self.results.put(('sweep',(stop,batch)))
            self.results.put(('sweep_done',stop))
        threading.Thread(target=work,daemon=True).start()

    def schedule_save(self):
        if self.save_job:self.window.after_cancel(self.save_job)
        self.save_job=self.window.after(1500,self.save_checks)

    def save_checks(self):
        self.save_job=None
        self.checks.save()

    def discard(self,rel):
        """Drop a file that is not a picture from the list, keeping the view where it is."""
        entry=next((e for e in self.entries if e.rel==rel),None)
        if entry is None:return
        self.entries.remove(entry)
        self.hidden+=1
        job=self.tasks.pop(entry,None)
        if job:job.cancel()
        self.cache.pop(entry,None)
        if entry in self.filtered:
            index=self.filtered.index(entry)
            self.filtered.pop(index)
            if self.selection is not None:
                if index<self.selection:self.selection-=1
                elif index==self.selection:
                    if self.selection>=len(self.filtered):self.selection=len(self.filtered)-1 if self.filtered else None
                    if self.preview:
                        if self.selection is None:self.close_preview()
                        else:self.path_label.set(self.filtered[self.selection].rel);self.schedule_preview()
            if self.preview:self.rail.reset()
            self.layout()
        self.show_status()

    def fake(self,entry):
        """A decode failure on something that is not even a picture file: remember it and hide it."""
        if looks_like_image(self.root/entry.rel):return False
        self.known[entry.rel]=[entry.modified,False]
        self.discard(entry.rel)
        self.schedule_save()
        return True

    def show_status(self):
        self.status.set(f'최신 수정순 · {len(self.filtered):,}장 / 전체 {len(self.entries):,}장'
                        +(f' · 이미지 아닌 파일 {self.hidden:,}개 숨김' if self.hidden else '')
                        +(f' · 접근 실패 {self.skipped}건' if self.skipped else ''))

    def schedule_filter(self,*_):
        if self.after_filter:self.window.after_cancel(self.after_filter)
        self.after_filter=self.window.after(120,self.filter)

    def filter(self,keep=None):
        """Rebuild the visible list; with `keep` (a view_anchor) return to that spot instead of the top."""
        if self.after_filter:self.window.after_cancel(self.after_filter)
        self.after_filter=None
        path,words=path_search(self.query.get(), self.root)
        if isinstance(path,str) and self.group.get()!=ALL and path.split(os.sep)[0]!=self.group.get().casefold():
            self.group.set(ALL)
        self.empty_text = ('현재 모아보는 폴더 밖의 경로입니다: '+str(self.root) if path is False
                           else '선택한 경로에 표시할 이미지가 없습니다.' if path is not None else '표시할 이미지가 없습니다.')
        group=self.group.get()
        self.filtered=[e for e in self.entries if (group==ALL or group==e.group)
                       and (self.dupes.get() or group!=ALL or (isinstance(path,str) and path.startswith('_겹친그림-지워도됨')) or not e.group.startswith('_겹친그림-지워도됨'))
                       and (path is None or (isinstance(path,str) and (path=='.' or e.key==path or e.key.startswith(path+os.sep))))
                       and all(w in e.key for w in words)]
        position={e.rel:i for i,e in enumerate(self.filtered)} if keep else {}
        selected=position.get(keep['sel']) if keep else None
        self.selection=selected
        self.path_label.set(self.filtered[selected].rel if selected is not None else '더블클릭: 사진 확대  ·  Ctrl+휠: 목록 크기')
        if self.preview:
            if selected is None:self.close_preview()
            else:
                self.rail.reset()
                if self.rail_open:self.rail.follow(selected)
                if self.filtered[selected].modified!=keep['modified']:self.schedule_preview()
        self.show_status()
        self.layout()
        top=position.get(keep['top']) if keep else None
        if top is None and selected is not None:top=selected
        rows=max(1,(len(self.filtered)+self.cols-1)//self.cols)
        self.cv.yview_moveto((top//self.cols)/rows if top is not None else 0)

    @property
    def cell_height(self):
        return round(125*self.card_scale)+49

    def scale_cards(self,e):
        old_top=max(0,int(self.cv.canvasy(0))//self.cell_height)*self.cols
        self.card_scale=max(.7,min(2.5,self.card_scale*(1.2 if e.delta>0 else 1/1.2)))
        for future in self.tasks.values():future.cancel()
        self.tasks.clear();self.cache.clear()
        self.set_columns(self.cv.winfo_width())
        total=max(1,(len(self.filtered)+self.cols-1)//self.cols)
        self.cv.yview_moveto((old_top//self.cols)/total)
        return 'break'

    def resize(self,e):
        self.set_columns(e.width)

    def set_columns(self,width):
        self.cols=max(1,width//round(203*self.card_scale))
        self.layout()

    def layout(self):
        height=((len(self.filtered)+self.cols-1)//self.cols)*self.cell_height
        self.cv.configure(scrollregion=(0,0,max(1,self.cv.winfo_width()),max(1,height)))
        self.schedule_paint()

    def scrolled(self,first,last):
        self.sb.set(first,last);self.schedule_paint()

    def schedule_paint(self):
        if not self.after_paint and not self.closed:self.after_paint=self.window.after_idle(self.paint)

    def paint(self):
        self.after_paint=None
        if self.preview:return
        self.cv.delete('all');self.keep.clear();self.visible.clear()
        y0=max(0,int(self.cv.canvasy(0))//self.cell_height)
        y1=int(self.cv.canvasy(self.cv.winfo_height()))//self.cell_height+2
        width=self.cv.winfo_width()/self.cols
        visible_entries=self.filtered[y0*self.cols:y1*self.cols]
        for e,f in list(self.tasks.items()):
            if e not in visible_entries and f.cancel():self.tasks.pop(e,None)
        for index in range(y0*self.cols,min(y1*self.cols,len(self.filtered))):
            e=self.filtered[index];row,col=divmod(index,self.cols)
            x=int(col*width);y=row*self.cell_height
            self.cv.create_rectangle(x+4,y+4,x+width-4,y+self.cell_height-3,fill=PANEL,
                                     outline='#77b4ec' if index==self.selection else BG)
            self.visible[e]=(x+width/2,y+round(64*self.card_scale))
            self.cv.create_text(x+10,y+self.cell_height-39,text=self.shorten(e.name,28),anchor='w',fill=FG)
            self.cv.create_text(x+10,y+self.cell_height-20,text=self.shorten(str(Path(e.rel).parent),32),anchor='w',fill=MUTED)
            if e in self.cache:
                self.cache.move_to_end(e);self.draw_thumb(e,self.cache[e])
            elif e not in self.tasks:
                f=self.pool.submit(load_picture,self.root/e.rel,tuple(round(n*self.card_scale) for n in THUMB));self.tasks[e]=f
                f.add_done_callback(lambda job,item=e:self.results.put(('thumb',(item,job))))
        if not self.filtered:self.cv.create_text(30,35,anchor='w',fill=FG,text=self.empty_text)

    @staticmethod
    def shorten(text,n):
        return text if len(text)<=n else text[:n-6]+'…'+text[-5:]

    def draw_thumb(self,e,image):
        if e not in self.visible:return
        if image is None:
            self.cv.create_text(*self.visible[e],text='미리보기 불가',fill=MUTED);return
        photo=ImageTk.PhotoImage(image);self.keep[e]=photo
        self.cv.create_image(*self.visible[e],image=photo)

    def click(self,event):
        self.cv.focus_set()
        x=int(event.x*self.cols/max(1,self.cv.winfo_width()))
        index=int(self.cv.canvasy(event.y)//self.cell_height)*self.cols+x
        if 0<=index<len(self.filtered):
            self.selection=index;self.path_label.set(self.filtered[index].rel);self.schedule_paint()

    def selected(self):
        if self.selection is not None and 0<=self.selection<len(self.filtered):
            return self.root/self.filtered[self.selection].rel

    def reveal(self):
        p=self.selected()
        if p and p.exists():subprocess.Popen(['explorer.exe','/select,',str(p)])
        elif p:messagebox.showinfo('원본 위치','파일이 이동됐어요. 새로고침해 주세요.',parent=self.window)

    def open_preview(self):
        if not self.selected():return
        if not self.preview:
            self.preview=ttk.Frame(self.gallery_area)
            self.preview.place(x=0,y=0,relwidth=1,relheight=1)
            self.preview.lift()
            nav=ttk.Frame(self.preview,padding=5)
            nav.pack(fill='x')
            self.rail_button=ttk.Button(nav,command=self.toggle_rail,width=12)
            self.rail_button.pack(side='left',padx=2)
            self.full_button=ttk.Button(nav,text='전체화면 (F)',width=14,command=self.toggle_fullscreen)
            self.full_button.pack(side='right',padx=2)
            for label,action in [('목록',self.close_preview),('◀',lambda:self.step(-1)),('▶',lambda:self.step(1)),
                                 ('−',lambda:self.zoom(1/1.25)),('+',lambda:self.zoom(1.25)),
                                 ('화면 맞춤',lambda:self.set_scale(None)),('100%',lambda:self.set_scale(1))]:
                ttk.Button(nav,text=label,width=max(3,len(label)*2),command=action).pack(side='left',padx=2)
            self.percent=tk.StringVar()
            ttk.Label(nav,textvariable=self.percent).pack(side='left',padx=6)
            ttk.Button(nav,text='원본 열기',command=self.original).pack(side='right')
            self.body=ttk.Frame(self.preview)
            self.body.pack(fill='both',expand=True)
            self.rail=ThumbnailRail(self.body,self)
            self.viewer=tk.Canvas(self.body,bg=BG,highlightthickness=0,takefocus=True)
            self.viewer.pack(side='right',fill='both',expand=True)
            self.image_item=self.viewer.create_image(0,0)
            self.show_rail()
            self.viewer.bind('<Configure>',lambda _:self.redraw_soon())
            self.viewer.bind('<MouseWheel>',lambda e:self.zoom(1.25 if e.delta>0 else .8,e.x,e.y))
            self.viewer.bind('<ButtonPress-1>',lambda e:setattr(self,'drag_at',(e.x,e.y)))
            self.viewer.bind('<B1-Motion>',self.pan)
            self.viewer.bind('<Double-Button-1>',lambda _:self.set_scale(None))
            self.preview_binds={key:self.window.bind(key,fn,add='+') for key,fn in [
                ('<Left>',lambda _:self.step(-1)),('<Right>',lambda _:self.step(1)),
                ('<KeyPress-f>',self.fullscreen_key),('<KeyPress-F>',self.fullscreen_key),
                ('<F11>',lambda _:self.toggle_fullscreen()),
                ('<Escape>',lambda _:self.toggle_fullscreen() if self.fullscreen else self.close_preview())]}
        self.viewer.focus_set()
        self.schedule_preview()

    def show_rail(self):
        self.rail_button.configure(text='◀ 미리보기 접기' if self.rail_open else '▶ 미리보기 열기')
        if self.rail_open:
            self.rail.pack(side='left',fill='y',before=self.viewer)
            self.rail.follow(self.selection)
        else:
            self.rail.pack_forget()

    def toggle_rail(self):
        self.rail_open=not self.rail_open
        self.show_rail()
        self.viewer.focus_set()

    def fullscreen_key(self,event):
        if isinstance(event.widget,(tk.Entry,ttk.Entry,ttk.Combobox)) or event.state & 0x000c:
            return
        return self.toggle_fullscreen()

    def toggle_fullscreen(self):
        try:
            self.fullscreen=self.frame_mode.switch()
        except OSError as exc:
            messagebox.showerror('전체화면',str(exc),parent=self.window)
            return 'break'
        if self.fullscreen:
            self.return_view=(self.preview_id,self.scale,self.center)
            for bar in (self.top_bar,self.filters_bar,self.bottom_bar):
                bar.pack_forget()
        else:
            self.top_bar.pack(fill='x',before=self.gallery_area)
            self.filters_bar.pack(fill='x',before=self.gallery_area)
            self.bottom_bar.pack(fill='x',after=self.gallery_area)
            if self.return_view and self.return_view[0]==self.preview_id:
                _,self.scale,self.center=self.return_view
            self.return_view=None
        self.full_button.configure(text='전체화면 종료 (F)' if self.fullscreen else '전체화면 (F)')
        self.viewer.focus_set()
        self.redraw_soon()
        return 'break'

    def schedule_preview(self):
        if not self.preview:return
        self.preview_id+=1
        token=self.preview_id
        if self.decode_job:self.decode_job.cancel()
        self.stop_animation()
        self.source_image=None
        self.large=None
        self.scale=None
        self.center=(0.,0.)
        self.percent.set('불러오는 중…')
        self.viewer.itemconfigure(self.image_item,image='')
        self.decode_job=self.pool.submit(original_pixels,self.selected())
        self.decode_job.add_done_callback(lambda job:self.results.put(('preview',(token,job))))

    def stop_animation(self):
        if self.animation_job:self.window.after_cancel(self.animation_job)
        self.animation_job=None
        self.frames=[];self.frame_at=0

    def show_frames(self,token,frames):
        self.frames,self.frame_at=frames,0
        self.source_image=frames[0][0]
        self.set_scale(None)
        if len(frames)>1:
            self.animation_job=self.window.after(frames[0][1],self.next_frame,token)

    def next_frame(self,token):
        self.animation_job=None
        if not self.preview or token!=self.preview_id or len(self.frames)<2:return
        self.frame_at=(self.frame_at+1)%len(self.frames)
        self.source_image,duration=self.frames[self.frame_at]
        self.redraw_soon()
        self.animation_job=self.window.after(duration,self.next_frame,token)

    def effective_scale(self):
        if self.scale is not None:return self.scale
        image=self.source_image
        return min(self.viewer.winfo_width()/image.width,self.viewer.winfo_height()/image.height,16. if self.fullscreen else 1.)

    def set_scale(self,value):
        self.return_view=None
        if self.source_image is None:return
        self.scale=value
        self.center=(self.source_image.width/2,self.source_image.height/2)
        self.redraw_soon()

    def zoom(self,factor,x=None,y=None):
        self.return_view=None
        if self.source_image is None:return 'break'
        w,h=self.viewer.winfo_width(),self.viewer.winfo_height()
        dx=(w/2 if x is None else x)-w/2
        dy=(h/2 if y is None else y)-h/2
        old=self.effective_scale()
        self.scale=max(min(.02,self.viewer.winfo_width()/self.source_image.width,self.viewer.winfo_height()/self.source_image.height),min(16.,old*factor))
        self.center=(self.center[0]+dx*(1/old-1/self.scale),
                     self.center[1]+dy*(1/old-1/self.scale))
        self.redraw_soon()
        return 'break'

    def pan(self,e):
        self.return_view=None
        if self.source_image is None:return
        x,y=self.drag_at
        scale=self.effective_scale()
        self.center=(self.center[0]+(x-e.x)/scale,self.center[1]+(y-e.y)/scale)
        self.drag_at=e.x,e.y
        self.redraw_soon()

    def redraw_soon(self):
        if self.preview_job is None and self.preview:
            self.preview_job=self.window.after_idle(self.draw_view)

    def draw_view(self):
        self.preview_job=None
        if not self.preview or self.source_image is None:return
        im=self.source_image
        w,h=max(1,self.viewer.winfo_width()),max(1,self.viewer.winfo_height())
        scale=self.effective_scale()
        # Resize only the visible source rectangle, even at 1600%.
        sw,sh=min(im.width,w/scale),min(im.height,h/scale)
        left=max(0.,min(im.width-sw,self.center[0]-sw/2))
        top=max(0.,min(im.height-sh,self.center[1]-sh/2))
        self.center=(left+sw/2,top+sh/2)
        animated=len(self.frames)>1
        pixels=im.resize((max(1,round(sw*scale)),max(1,round(sh*scale))),
                         Image.Resampling.BILINEAR if animated else Image.Resampling.LANCZOS,
                         box=(left,top,min(im.width,left+sw),min(im.height,top+sh)))
        new_image=ImageTk.PhotoImage(pixels)
        self.viewer.itemconfigure(self.image_item,image=new_image)
        self.viewer.coords(self.image_item,w/2,h/2)
        self.large=new_image
        self.percent.set(f'{scale:.0%} · {im.width}×{im.height}'
                         +(f' · 움직이는 그림 {len(self.frames)}장' if animated else ''))
        self.path_label.set('휠: 확대/축소  ·  드래그: 이동  ·  Esc: 목록    '+self.filtered[self.selection].rel)

    def step(self,amount):
        self.jump(self.selection+amount)

    def jump(self,index):
        if 0<=index<len(self.filtered):
            self.selection=index;self.path_label.set(self.filtered[index].rel)
            first=int(self.cv.canvasy(0));y=index//self.cols*self.cell_height
            if y<first or y+self.cell_height>first+self.cv.winfo_height():
                self.cv.yview_moveto(y/max(1,((len(self.filtered)+self.cols-1)//self.cols)*self.cell_height))
            if self.rail_open:self.rail.follow(index)
            self.schedule_paint();self.schedule_preview()

    def original(self):
        try:os.startfile(str(self.selected()))
        except OSError as exc:messagebox.showerror('원본 열기',str(exc),parent=self.preview)

    def close_preview(self):
        if self.fullscreen:self.toggle_fullscreen()
        self.rail.dispose()
        self.preview_id+=1
        if self.preview_job:self.window.after_cancel(self.preview_job);self.preview_job=None
        if self.decode_job:self.decode_job.cancel()
        self.stop_animation()
        for key,binding in self.preview_binds.items():self.window.unbind(key,binding)
        self.preview.destroy();self.preview=None;self.large=None;self.source_image=None
        self.cv.focus_set();self.schedule_paint()

    def poll(self):
        if self.closed:return
        for _ in range(80):
            try:kind,value=self.results.get_nowait()
            except queue.Empty:break
            if kind=='scan':
                stop,value=value
                if stop is not self.scan_stop:continue
                self.scan_busy=False;self.refresh.configure(state='normal')
                self.entries,self.skipped,self.hidden=value
                available=sorted({e.group for e in self.entries},key=str.casefold)
                self.groups.configure(values=[ALL]+available)
                if any(g.startswith('_겹친그림-지워도됨') for g in available):
                    self.dupe_toggle.pack(side='right')
                else:self.dupe_toggle.pack_forget()
                if self.group.get() not in available:self.group.set(ALL)
                self.filter(keep=self.anchor);self.anchor=None
                self.sweep()
            elif kind=='scan_error':
                stop,value=value
                if stop is not self.scan_stop:continue
                self.scan_busy=False;self.refresh.configure(state='normal');self.status.set(value)
            elif kind=='sweep':
                stop,batch=value
                if stop is not self.scan_stop:continue
                for rel,modified,ok in batch:
                    self.known[rel]=[modified,ok]
                    if not ok:self.discard(rel)
                if batch:self.schedule_save()
            elif kind=='sweep_done':
                if value is self.scan_stop:self.schedule_save()
            elif kind=='thumb':
                e,job=value
                if self.tasks.get(e) is not job:continue
                del self.tasks[e]
                try:picture=job.result()[0]
                except Exception:picture=None
                if picture is None and self.fake(e):continue
                self.cache[e]=picture
                if len(self.cache)>100:self.cache.popitem(last=False)
                self.draw_thumb(e,picture)
            elif kind=='rail':
                rail,index,job=value
                if self.preview and rail is self.rail:rail.finish(index,job)
            elif kind=='preview':
                token,job=value
                if self.preview and token==self.preview_id:
                    try:frames=job.result()
                    except Exception:
                        if not self.fake(self.filtered[self.selection]):
                            self.percent.set('미리보기 불가 · 원본 열기로 확인하세요.')
                    else:self.show_frames(token,frames)
        self.window.after(35,self.poll)

    def close(self):
        self.closed=True
        if self.scan_stop:self.scan_stop.set()
        if self.save_job:self.window.after_cancel(self.save_job);self.checks.save()
        self.pool.shutdown(wait=False,cancel_futures=True)
        self.window.destroy()


def create_window():
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('CHARTER.ResearchInbox.Gallery')
    window=tk.Tk()
    icon = str(Path(__file__).with_name('gallery.ico').resolve())
    window.iconbitmap(default=icon)
    window.iconbitmap(icon)
    return window


if __name__=='__main__':
    window=create_window()
    window.withdraw()
    target=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else None
    if target is not None and not target.is_dir():
        messagebox.showerror('이미지 모아보기',f'폴더를 찾을 수 없어요.\n{target}',parent=window)
        target=None
    if target is None:
        chosen=filedialog.askdirectory(parent=window,title='이미지를 모아볼 폴더 선택',mustexist=True)
        target=Path(chosen).resolve() if chosen else None
    if target is not None:
        Gallery(window,target);window.deiconify();window.mainloop()
    else:
        window.destroy()
