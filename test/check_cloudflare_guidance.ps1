$ErrorActionPreference = 'Stop'
$response = Invoke-WebRequest -Uri 'https://developers.cloudflare.com/cloudflare-challenges/reference/supported-browsers/' -Headers @{Accept='text/markdown'} -TimeoutSec 20
$target = Join-Path $PSScriptRoot 'out/cloudflare_supported_browsers.txt'
[System.IO.File]::WriteAllText($target, $response.Content, [System.Text.UTF8Encoding]::new($false))
Write-Output ("SOURCE " + $response.BaseResponse.RequestMessage.RequestUri.AbsoluteUri)
Select-String -LiteralPath $target -Pattern 'Playwright|Selenium|automated|headless' -Context 1,1
