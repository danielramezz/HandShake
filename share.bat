@echo off
REM Double-click to start Handshake WITH remote access:
REM a public https://...trycloudflare.com link for friends on other networks.
REM (start.bat alone = phones on the same Wi-Fi only.)
call "%~dp0start.bat" --share
echo.
echo Server stopped.
pause
