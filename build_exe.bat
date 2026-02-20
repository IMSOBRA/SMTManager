@echo off
chcp 65001 >nul
echo ========================================================
echo SMT Manager v2.0 - 파이썬 위치 추적 빌드
echo ========================================================
echo.
echo 파이썬이 어디 숨어있는지 프로그램이 직접 찾아보는 중입니다...

set "PYTHON_CMD="

:: 1. 일반 python 명령어로 확인
python --version >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "PYTHON_CMD=python"
    goto found
)

:: 2. py 런처 확인
py --version >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "PYTHON_CMD=py"
    goto found
)

:: 3. 대부분 설치되는 C 드라이브 경로들을 이잡듯이 뒤지기
for /d %%I in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
    if exist "%%I\python.exe" (
        set "PYTHON_CMD=%%I\python.exe"
        goto found
    )
)

for /d %%I in ("C:\Program Files\Python*") do (
    if exist "%%I\python.exe" (
        set "PYTHON_CMD=%%I\python.exe"
        goto found
    )
)

:: 4. 아나콘다(Anaconda)로 설치된 경우 확인
if exist "C:\ProgramData\Anaconda3\python.exe" (
    set "PYTHON_CMD=C:\ProgramData\Anaconda3\python.exe"
    goto found
)

if exist "%USERPROFILE%\anaconda3\python.exe" (
    set "PYTHON_CMD=%USERPROFILE%\anaconda3\python.exe"
    goto found
)

:not_found
echo.
echo ========================================================
echo [오류] 자동 검색 실패 ㅠㅠ
echo 도저히 컴퓨터에서 파이썬을 찾지 못했습니다!
echo.
echo 혹시 파이썬이 안 깔려있거나, 마이크로소프트 스토어로 까셨나요?
echo 파이썬이 없으시다면 https://www.python.org/downloads/ 에서
echo [Python 3.x 버전] 다운로드 후 설치해주셔야 EXE 파일로 만들 수 있습니다!
echo ========================================================
pause
exit /b

:found
echo.
echo [야호!] 파이썬을 여기서 찾았습니다!
echo 경로: %PYTHON_CMD%
echo.

echo 1. 필요한 부품(패키지) 설정 중... (조금 오래 걸립니다)
"%PYTHON_CMD%" -m pip install pypiwin32 pyinstaller psutil pystray pillow

echo.
echo 2. 드디어 SMTManager.exe 파일 만들기 시작! (이것도 1~2분 소요)
"%PYTHON_CMD%" -m PyInstaller --noconfirm --onefile --windowed --name "SMTManager" SMTManager.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo ========================================================
    echo [오류] 뭔가 잘못되었습니다!
    echo 화면에 빨간 글씨나 에러라고 뜬 부분을 복사해서 저에게 알려주세요!
    echo ========================================================
    pause
    exit /b
)

echo.
echo ========================================================
echo 3. 대성공! 🎉 드디어 빌드가 완료되었습니다!
echo 지금 보고 계신 폴더 안에 새로 생긴 [dist] 폴더로 들어가보세요.
echo 거기에 배포하실 수 있는 SMTManager.exe 파일이 예쁘게 들어있습니다.
echo ========================================================
echo.
pause
