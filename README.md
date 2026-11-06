# 이미지 모아보기

폴더 하나를 고르면 그 안의 모든 하위 폴더 사진을 **최신 수정순**으로 한 화면에 모아 보여 주는 Windows 용 뷰어입니다. 파일을 옮기거나 지우지 않는 읽기 전용 프로그램입니다.

## 다운로드

[Releases](https://github.com/supershopcover/image-gallery/releases/latest) 에서 `image-gallery-<버전>-Windows-x64.zip` 을 받아 압축을 풀고 `이미지 모아보기.exe` 를 실행하세요. 설치 과정은 없습니다.

처음 실행할 때 Windows SmartScreen 이 "알 수 없는 게시자" 경고를 띄울 수 있습니다. **추가 정보 → 실행**을 누르면 됩니다. 코드 서명을 하지 않은 개인 배포라 나오는 경고입니다.

## 기능

- 하위 폴더까지 전부 훑어 최신 수정순으로 나열, 폴더별 필터와 파일·경로 검색
- 더블클릭으로 크게 보기: 휠 확대/축소, 드래그 이동, 왼쪽 미리보기 레일로 빠르게 이동
- 움직이는 GIF / WebP 는 크게 보기에서 재생
- 이름만 이미지이고 내용은 웹페이지인 가짜 파일은 자동으로 숨기고 개수를 표시
- 새로고침(F5) 해도 보고 있던 자리와 선택한 사진이 그대로 유지
- 전체화면(F / F11), Ctrl+휠로 목록 썸네일 크기 조절
- 지원 형식: JPEG, PNG, GIF, WebP, BMP, TIFF, AVIF

## 단축키

| 키 | 동작 |
|---|---|
| 더블클릭 / Enter | 크게 보기 |
| ← → | 이전 / 다음 사진 |
| 휠 | 확대 / 축소 (크게 보기) |
| Ctrl + 휠 | 목록 썸네일 크기 |
| F, F11 | 전체화면 |
| Esc | 전체화면 종료 또는 목록으로 |
| F5 | 새로고침 |
| Ctrl + O | 다른 폴더 열기 |

## 소스로 실행

Python 3.12 와 Pillow 가 필요합니다.

```bash
pip install Pillow
pythonw inbox_gallery.pyw "D:\사진"
```

폴더 인자를 생략하면 폴더 선택 창이 뜹니다. 탐색기에서 폴더를 `.pyw` 파일이나 exe 위에 끌어놓아도 됩니다.

가짜 이미지 검사 결과는 `%LOCALAPPDATA%\이미지 모아보기\checked.json` 에 저장됩니다. 지우면 다음 실행 때 다시 검사합니다.

## 빌드

```bash
pip install Pillow==12.1.1 pyinstaller==6.22.3
python build_gallery.py "이미지 모아보기-Windows-x64.zip"
```

Windows x64 에서만 빌드됩니다. 결과 zip 에는 exe, 사용법, 그리고 Python · Tk · Pillow · PyInstaller 라이선스가 함께 들어갑니다.

## 라이선스

MIT
