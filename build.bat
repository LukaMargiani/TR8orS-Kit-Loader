@echo off
rem Builds dist\TR8orS-Kit-Loader.exe (one file, no console window).
python -m PyInstaller --noconfirm --clean --onefile --windowed --name TR8orS-Kit-Loader --collect-all tkinterdnd2 app.py
