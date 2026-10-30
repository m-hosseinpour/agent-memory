@echo off
rem Launcher for the `mem` CLI on Windows, without installing the package.
rem Mirrors bin/mem: puts the checkout on PYTHONPATH and runs the module.
setlocal
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
python -m memlib %*
