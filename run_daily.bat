@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" "collectors\mavir_kiegyenlito.py" >> "logs\run_daily_stdout.log" 2>&1
timeout /t 5 >nul
".venv\Scripts\python.exe" "collectors\mavir_rendszerallapot_realtime.py" >> "logs\run_daily_stdout.log" 2>&1
timeout /t 5 >nul
".venv\Scripts\python.exe" "collectors\mavir_frekvencia.py" >> "logs\run_daily_stdout.log" 2>&1
timeout /t 5 >nul
".venv\Scripts\python.exe" "collectors\hupx_dam.py" >> "logs\run_daily_stdout.log" 2>&1
timeout /t 5 >nul
".venv\Scripts\python.exe" "collectors\hupx_idc.py" >> "logs\run_daily_stdout.log" 2>&1
timeout /t 5 >nul
".venv\Scripts\python.exe" "collectors\hupx_ida.py" >> "logs\run_daily_stdout.log" 2>&1
timeout /t 5 >nul
".venv\Scripts\python.exe" "collectors\weather.py" >> "logs\run_daily_stdout.log" 2>&1
