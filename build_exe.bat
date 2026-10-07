@echo off
rem Builds a standalone dist\PolarH7_LSL.exe (no Python needed to run it).
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --onefile --console --name PolarH7_LSL --collect-all pylsl --collect-all bleak --collect-all winrt --distpath dist --workpath build --specpath build polar_h7_lsl.py
