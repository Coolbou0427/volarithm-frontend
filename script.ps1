# Run the web server (PowerShell)
$env:PYTHONPATH = "$PSScriptRoot\..;$env:PYTHONPATH"
python -m web-interface.server
