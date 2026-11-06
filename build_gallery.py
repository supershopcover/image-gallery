"""Windows x64: build with Python 3.12, Pillow 12.1.1 and PyInstaller 6.22.3."""
from importlib.metadata import distribution
from pathlib import Path
import argparse
import platform
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
NAME = '이미지 모아보기'
GUIDE = """이미지 모아보기 · Windows x64

압축을 풀고 '이미지 모아보기.exe'를 실행한 다음 사진이 있는 폴더를 선택하세요.
선택한 폴더와 하위 폴더의 사진을 최신 수정순으로 표시합니다.
탐색기에서 폴더 하나를 실행 파일 위에 끌어놓아도 됩니다.

사진 더블클릭: 크게 보기
휠: 사진 확대/축소   |   드래그: 사진 이동
왼쪽 미리보기 또는 방향키: 다른 사진으로 이동
F 또는 F11: 전체화면 전환   |   Esc: 전체화면 종료 또는 목록으로 돌아가기
Ctrl+휠: 목록에서 썸네일 크기 조절
폴더 열기 또는 Ctrl+O: 다른 폴더 선택

지원 형식: JPEG, PNG, GIF, WebP, BMP, TIFF, AVIF. 움직이는 GIF/WebP 는 크게 보기에서 재생되고, 목록 썸네일은 첫 장을 표시합니다.
이름만 이미지이고 내용은 웹페이지인 파일은 목록에서 숨기고, 상단에 숨긴 개수를 표시합니다.
새로고침(F5)은 보고 있던 자리와 선택한 사진을 그대로 유지합니다.
"""
def build(target):
    if sys.platform != 'win32' or platform.machine().upper() != 'AMD64':
        raise SystemExit('Windows x64 환경에서 실행하세요.')
    target.parent.mkdir(parents=True,exist_ok=True)
    # Keep the completed archive on the same volume for an atomic replacement.
    with tempfile.TemporaryDirectory(dir=target.parent) as temp:
        stage=Path(temp)
        subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--onefile','--windowed',
                        '--name',NAME,'--icon',str(ROOT/'gallery.ico'),
                        '--add-data',f"{ROOT/'gallery.ico'};.","--specpath",str(stage),
                        '--distpath',str(stage/'dist'),'--workpath',str(stage/'build'),
                        str(ROOT/'inbox_gallery.pyw')],check=True)
        archive=stage/'result.zip'
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as out:
            out.write(stage/'dist'/(NAME+'.exe'),NAME+'.exe')
            out.writestr('사용법.txt',GUIDE.replace('\n','\r\n').encode('utf-8-sig'))
            base=Path(sys.base_prefix)
            out.write(base/'LICENSE.txt','licenses/Python.txt')
            out.write(base/'tcl/tk8.6/license.terms','licenses/Tk.txt')
            for pkg in ('Pillow','PyInstaller'):
                dist=distribution(pkg)
                for file in dist.files or []:
                    if '.dist-info/licenses/' in file.as_posix():
                        name=file.as_posix().split('.dist-info/licenses/',1)[1]
                        out.write(dist.locate_file(file),'licenses/'+pkg+'/'+name)
        archive.replace(target)
if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('zip',type=Path)
    build(parser.parse_args().zip.resolve())

