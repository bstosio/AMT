@echo off
rem Buduje AMT.exe - maly program startowy z ikona aplikacji.
rem Wystarczy Windows: kompilator C# jest czescia .NET Framework 4.
setlocal
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
"%CSC%" /nologo /target:winexe /optimize+ /codepage:65001 /win32icon:"%~dp0..\assets\amt.ico" /r:System.Windows.Forms.dll /out:"%~dp0..\AMT.exe" "%~dp0launcher.cs"
if errorlevel 1 (echo Blad budowania & exit /b 1)
echo Zbudowano: %~dp0..\AMT.exe
