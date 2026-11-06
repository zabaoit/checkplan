"""Offline headed Edge visibility check. Own window only; no account or network."""
import asyncio
import ctypes
import json
import sys
from ctypes import wintypes
from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding="utf-8")
TITLE = "Kiem tra Change mail"
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
user32.GetThreadDesktop.restype = wintypes.HANDLE
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [CALLBACK, wintypes.LPARAM]


def desktop_name(handle):
    name = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD()
    if not handle or not user32.GetUserObjectInformationW(handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
        return {"error": ctypes.get_last_error()}
    return name.value


def visibility():
    windows = []

    @CALLBACK
    def collect(hwnd, _):
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, len(title))
        if TITLE in title.value:
            pid = wintypes.DWORD()
            tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            windows.append({"pid": pid.value, "visible": bool(user32.IsWindowVisible(hwnd)),
                            "minimized": bool(user32.IsIconic(hwnd)),
                            "desktop": desktop_name(user32.GetThreadDesktop(tid))})
        return True

    user32.EnumWindows(collect, 0)
    input_desktop = user32.OpenInputDesktop(0, False, 1)
    try:
        return {"current_desktop": desktop_name(user32.GetThreadDesktop(kernel32.GetCurrentThreadId())),
                "input_desktop": desktop_name(input_desktop), "own_windows": windows}
    finally:
        if input_desktop:
            user32.CloseDesktop(input_desktop)


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=False, args=["--start-maximized"])
        try:
            context = await browser.new_context(no_viewport=True)
            page = await context.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content('<title>' + TITLE + '</title><h1>Kiểm tra cửa sổ Change mail</h1><p>Trang kiểm tra ngoại tuyến. Không đăng nhập, không đổi email.</p>')
            await page.bring_to_front()
            print("BROWSER_VISIBILITY", json.dumps(visibility(), ensure_ascii=False), flush=True)
            await page.wait_for_event("close", timeout=0)
        finally:
            await browser.close()


asyncio.run(main())
