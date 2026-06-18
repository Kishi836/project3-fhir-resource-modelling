@echo off
setlocal
cd /d "%~dp0"

REM Submission is an idempotent upsert (one ABHA = one Patient), so re-running is
REM safe and never creates duplicates. If a shared sandbox already holds duplicate
REM patients from older runs, clean them up ONCE first (run manually, not here):
REM     python cleanup_sandbox.py --dry-run
REM     python cleanup_sandbox.py

echo ============================================
echo  STEP 1: Submitting FHIR resources to HAPI server (upsert)...
echo ============================================
python validate_resources.py
if errorlevel 1 (
    echo.
    echo Step 1 failed. Aborting.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  STEP 2: Fetching data back and building timeline...
echo ============================================
python timeline_builder.py auto
if errorlevel 1 (
    echo.
    echo Step 2 failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  STEP 3: Building the Word project report...
echo ============================================
python build_report.py
if errorlevel 1 (
    echo.
    echo Step 3 failed (is python-docx installed? pip install -r requirements.txt)
    pause
    exit /b 1
)

echo.
echo ============================================
echo  DONE! Results saved in the output\ folder:
echo    - validation_report.txt
echo    - id_map.json
echo    - patient_timeline.json
echo    - Project3_FHIR_Report.docx
echo ============================================
pause
