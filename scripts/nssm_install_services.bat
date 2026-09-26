@echo off
REM NSSM Windows Service Registration for QuantumPartners Prop Trading System
REM Mendaftarkan 2 Windows Service independen: QP-Watchdog dan QP-TradingEngine

SET NSSM_EXE=tools\nssm.exe
SET APP_DIR=%~dp0..
SET PYTHON_EXE=%APP_DIR%\.venv\Scripts\python.exe

echo ====================================================================
echo Registering Windows Services for QuantumPartners Prop Trading
echo ====================================================================

REM 1. Register Risk Watchdog Service
%NSSM_EXE% install QP-Watchdog "%PYTHON_EXE%" "services\risk_watchdog.py"
%NSSM_EXE% set QP-Watchdog AppDirectory "%APP_DIR%"
%NSSM_EXE% set QP-Watchdog DisplayName "QuantumPartners Risk Watchdog"
%NSSM_EXE% set QP-Watchdog Description "Out-of-Band Risk & Equity Watchdog"
%NSSM_EXE% set QP-Watchdog Start SERVICE_AUTO_START
%NSSM_EXE% set QP-Watchdog AppRestartDelay 3000

REM 2. Register Trading Engine Service (Depend on Watchdog)
%NSSM_EXE% install QP-Engine "%PYTHON_EXE%" "services\trading_engine.py"
%NSSM_EXE% set QP-Engine AppDirectory "%APP_DIR%"
%NSSM_EXE% set QP-Engine DisplayName "QuantumPartners Trading Engine"
%NSSM_EXE% set QP-Engine Description "Core OMS and Execution Engine"
%NSSM_EXE% set QP-Engine Start SERVICE_AUTO_START
%NSSM_EXE% set QP-Engine DependOnService QP-Watchdog
%NSSM_EXE% set QP-Engine AppRestartDelay 3000

echo Services registered successfully!
echo To start: net start QP-Watchdog && net start QP-Engine
