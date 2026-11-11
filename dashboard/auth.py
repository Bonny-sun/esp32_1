#!/usr/bin/env python3
"""
╔═══════════════════════════════════ 檔案導覽 ═══════════════════════════════════╗

類別：後端（Backend）── /admin、/ask 登入表單用的安全邏輯，純函式/類別，沒有畫面。
目的：兩件事
      1. 密碼驗證：拿使用者輸入的密碼跟 bcrypt 雜湊值比對，絕不比對明碼
      2. 登入失敗限流：同一瀏覽器連續猜錯太多次就鎖一段時間，防止暴力破解，
         每次失敗跟每次被擋下的嘗試都會印出一行紀錄（Render 的 log 看得到）
獨立成這支檔案（沒有放進 app.py）：是為了能在不載入 NiceGUI 頁面的情況下被
測試——跟 ask/tools.py、ask/agent.py 分離出來的理由一樣（見 tests/conftest.py
的說明：匯入 app.py 會順便觸發它裡面近千行的 NiceGUI 頁面註冊）。

程式架構：
  一、密碼驗證          【1-1】check_password()
  二、登入失敗限流       【2-1】限流參數設定  【2-2】LoginLockout 類別

備註：
  改失敗幾次才鎖 ........ 【2-1】的 LOGIN_MAX_ATTEMPTS（環境變數）
  改鎖多久 .............. 【2-1】的 LOGIN_LOCKOUT_SECONDS（環境變數）

╚════════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

import bcrypt

# ============================================================================
# 一、密碼驗證
# ============================================================================


# ---- 【1-1】拿明碼跟 bcrypt 雜湊值比對；雜湊值是空的或格式壞掉都視為「不通過」----
def check_password(candidate: str, password_hash: str) -> bool:
    """bcrypt-verify `candidate` against `password_hash`. Fails closed
    (False) on an empty or malformed hash — never falls back to a
    plaintext comparison, so a misconfigured env var can't accidentally
    reopen the plaintext path."""
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(candidate.encode(), password_hash.encode())
    except ValueError:
        return False


# ============================================================================
# 二、登入失敗限流：防止 /admin、/ask 的登入表單被暴力破解密碼
# ============================================================================

# ---- 【2-1】限流參數：預設同一身分連續失敗 5 次鎖 15 分鐘；另外設全站 20 次 ----
# 上限，防止攻擊者靠清瀏覽器 cookie 繞過個人鎖定。存在記憶體裡（跟 ask 的
# RateLimiter 同樣的取捨），重新部署會歸零，對單一管理者的個人系統可接受。
@dataclass
class LoginLockoutConfig:
    max_attempts: int = field(default_factory=lambda: int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5")))
    site_max_attempts: int = field(default_factory=lambda: int(os.environ.get("LOGIN_SITE_MAX_ATTEMPTS", "20")))
    window_seconds: int = field(default_factory=lambda: int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "900")))


# ---- 【2-2】限流器本體：check 檢查能不能再試、record_failure/record_success 記錄結果 ----
class LoginLockout:
    def __init__(self, config: LoginLockoutConfig | None = None, *, now: Callable[[], float] = time.time) -> None:
        self._config = config or LoginLockoutConfig()
        self._now = now
        self._per_identity: dict[str, list[float]] = {}
        self._site: list[float] = []

    @staticmethod
    def _prune(timestamps: list[float], cutoff: float) -> list[float]:
        return [t for t in timestamps if t > cutoff]

    def check(self, identity: str) -> str | None:
        """None = allowed to try; otherwise a ready-to-show 繁體中文 reason.
        Logs a blocked attempt (print, not just the UI notice) — this is
        the stronger signal of the two: someone still trying after already
        being locked out, as opposed to an ordinary wrong-password typo."""
        now = self._now()
        cutoff = now - self._config.window_seconds
        minutes = self._config.window_seconds // 60
        if len(self._prune(self._per_identity.get(identity, []), cutoff)) >= self._config.max_attempts:
            print(f"[auth] login blocked: identity={identity} over per-identity limit", flush=True)
            return f"登入失敗次數過多，請 {minutes} 分鐘後再試。"
        if len(self._prune(self._site, cutoff)) >= self._config.site_max_attempts:
            print(f"[auth] login blocked: site-wide limit reached (identity={identity})", flush=True)
            return "登入嘗試次數異常，請稍後再試。"
        return None

    def record_failure(self, identity: str) -> None:
        now = self._now()
        pruned = self._prune(self._per_identity.get(identity, []), now - self._config.window_seconds)
        pruned.append(now)
        self._per_identity[identity] = pruned
        self._site = self._prune(self._site, now - self._config.window_seconds)
        self._site.append(now)
        print(
            f"[auth] login failed: identity={identity} ({len(pruned)}/{self._config.max_attempts} "
            f"in this window)",
            flush=True,
        )

    def record_success(self, identity: str) -> None:
        self._per_identity.pop(identity, None)

    def describe(self) -> str:
        """Ready-to-show 繁體中文 hint for the login form itself, so the
        policy shown to the user always matches what's actually enforced
        (built from the live config, never a hardcoded string the caller
        could forget to update after changing LOGIN_MAX_ATTEMPTS/
        LOGIN_LOCKOUT_SECONDS)."""
        minutes = self._config.window_seconds // 60
        return f"密碼輸入錯誤達 {self._config.max_attempts} 次將鎖定登入 {minutes} 分鐘"
