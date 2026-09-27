@echo off
cd /d "%~dp0"
python "%~dp0boot_force_pin_helper.py"
if errorlevel 1 pause
