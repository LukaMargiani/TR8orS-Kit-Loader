@echo off
rem Builds dist\TR8orS-Kit-Loader.exe (one file, no console window).
rem The excludes keep large packages that may be installed in the same Python out of the exe; the app uses none of them.
set EXCLUDE=
for %%m in (torch tensorflow matplotlib pandas IPython jupyter notebook lxml sklearn sympy numba llvmlite PIL pyarrow librosa transformers PyQt5 PyQt6 PySide6 cv2 dask) do call set EXCLUDE=%%EXCLUDE%% --exclude-module %%m
python -m PyInstaller --noconfirm --clean --onefile --windowed --name TR8orS-Kit-Loader --collect-all tkinterdnd2 %EXCLUDE% app.py
