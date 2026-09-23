@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" "collectors\mavir_kiegyenlito.py" >> "logs\run_daily_stdout.log" 2>&1
".venv\Scripts\python.exe" "collectors\mavir_rendszerallapot_realtime.py" >> "logs\run_daily_stdout.log" 2>&1
".venv\Scripts\python.exe" "collectors\mavir_frekvencia.py" >> "logs\run_daily_stdout.log" 2>&1
