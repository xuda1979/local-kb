#!/usr/bin/env python3
"""browser_e2e.py — real-Firefox IndexedDB end-to-end test.

Drives the offline demo page (public/index.html) headlessly through geckodriver:

  AC1  create/edit/delete docs, reopen persists (IndexedDB)
  AC2  no plaintext in IndexedDB (inspect raw records)
  AC3  no access without the correct master password
  AC6  pure offline demo page (no server dependency beyond static files)

Run:  .venv/bin/python tests/browser_e2e.py
Prereq: firefox + geckodriver on PATH; start the demo server first
       (node --enable-source-maps server/demo.mjs &)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

BASE = os.environ.get("KB_BASE", "http://127.0.0.1:4242")


def find_firefox_binary():
    """Locate a Firefox executable usable by geckodriver/selenium.

    On Ubuntu, /usr/bin/firefox is a wrapper script for the snap, which
    selenium rejects ("binary is not a Firefox executable"). Allow an
    explicit override and fall back to the snap's inner binary.
    """
    candidates = []
    if os.environ.get("FIREFOX_BIN"):
        candidates.append(os.environ["FIREFOX_BIN"])
    import glob as _glob

    candidates.extend(
        sorted(
            _glob.glob("/snap/firefox/*/usr/lib/firefox/firefox"),
            key=os.path.getmtime,
            reverse=True,
        )
    )
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


KNOWN_TITLE = "Project Snowstorm Operations"
KNOWN_BODY = "The rendezvous coordinates are 48.8584N, 2.2945E; access code oscar-7."
KNOWN_EDIT = "UPDATED: deflector polarity reversed."


def wait_for(driver, fn, timeout=10):
    return WebDriverWait(driver, timeout).until(fn)


def fresh_profile():
    opts = Options()
    opts.headless = True
    opts.add_argument("-profile")
    import tempfile, shutil
    tmp = tempfile.mkdtemp(prefix="ffkb-")
    opts.add_argument(tmp)
    # In-memory-only profile storage so tests are deterministic
    prefs = {
        "profile.content_settings.exceptions.automatic_downloads": "*",
    }
    return opts, tmp


def get_db_dump(driver):
    """Serialize every IndexedDB record in the page's DB (local-kb)."""
    script = """
    const cb = arguments[arguments.length - 1];
    (async () => {
      const req = indexedDB.open('local-kb');
      const db = await new Promise((res, rej) => {
        req.onsuccess = () => res(req.result);
        req.onerror = () => { res(null); };
      });
      if (!db) return cb({ error: true });
      const names = Array.from(db.objectStoreNames);
      const all = {};
      for (const n of names) {
        all[n] = await new Promise((res) => {
          const tx = db.transaction(n, 'readonly');
          const q = tx.objectStore(n).getAll();
          q.onsuccess = () => res(q.result);
          q.onerror = () => res([]);
        });
      }
      db.close();
      cb(all);
    })().catch((e) => cb({ error: true, msg: String(e) }));
    """
    return driver.execute_async_script(script)


def flatten_dump(d):
    """All string values from every store, concatenated into one blob."""
    parts = []
    for name, recs in d.items():
        if not isinstance(recs, list):
            continue
        for r in recs:
            if isinstance(r, dict):
                for k, v in r.items():
                    parts.append(str(v))
            else:
                parts.append(str(r))
    return "\n".join(parts)


def main():
    print("==> launching Firefox (headless) against", BASE)
    opts = Options()
    opts.headless = True
    fb = find_firefox_binary()
    if fb is None:
        print("! no Firefox binary found; refusing to run")
        sys.exit(2)
    opts.binary_location = fb  # snap wrapper /usr/bin/firefox is not binary
    opts.set_preference("dom.indexedDB.enabled", True)
    driver = webdriver.Firefox(options=opts)
    driver.set_page_load_timeout(20)

    failures = []

    def check(name, cond, detail=""):
        if cond:
            print(f"  ok  {name}")
        else:
            print(f"FAIL  {name}  {detail}")
            failures.append(name)

    try:
        driver.get(BASE + "/")
        # wait for main.js to have run: the unlock view becomes visible only
        # after the module executes and calls setView() (CSS hides all .view)
        wait_for(
            driver,
            EC.visibility_of_element_located((By.ID, "view-unlock")),
        )
        check("demo page loads offline (unlock view present)", True)

        # ---- AC3: no access without password
        driver.find_element(By.ID, "btn-unlock").click()
        time.sleep(1.2)
        log = driver.find_element(By.ID, "log").text
        check(
            "unlock with empty password is blocked",
            "password" in log.lower() and "master" in log.lower(),
            log,
        )

        # ---- create-time master-password gate (用户手册 3.1 requires setting
        # the master password "并再次确认"; 4.1 可配置项 lists 主密码 "≥ 8 字符").
        # A too-short password must be refused before any key derivation runs.
        sn_short = driver.find_element(By.ID, "wsname")
        sn_short.send_keys("ops-vault")
        pw_short = driver.find_element(By.ID, "pw2")
        pw_short.send_keys("short1")  # 7 chars — below the 8-char floor
        cfm_short = driver.find_element(By.ID, "pw2b")
        cfm_short.send_keys("short1")
        driver.find_element(By.ID, "btn-create").click()
        time.sleep(0.8)
        log = driver.find_element(By.ID, "log").text
        check("create refused when the master password is shorter than 8 chars",
              "at least 8" in log.lower(), log)
        sn_short.clear()
        pw_short.clear()
        cfm_short.clear()

        # ---- create workspace (confirm field: a typo is unrecoverable)
        wsname = driver.find_element(By.ID, "wsname")
        wsname.send_keys("ops-vault")
        pw = driver.find_element(By.ID, "pw2")
        pw.send_keys("correct horse battery staple")
        mismatch = driver.find_element(By.ID, "pw2b")
        mismatch.send_keys("correct horse battery staple but different")
        driver.find_element(By.ID, "btn-create").click()
        time.sleep(0.8)
        log = driver.find_element(By.ID, "log").text
        check(
            "create refused when the two passwords differ",
            "do not match" in log.lower(),
            log,
        )
        mismatch.clear()
        mismatch.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-create").click()
        # view-app exists in static HTML but is display:none until the async
        # PBKDF2 (310k iters) key derivation finishes and setView() runs — wait
        # for actual visibility, not DOM presence.
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        check("workspace created, app view shown", True)
        label = driver.find_element(By.ID, "ws-label").text
        check("workspace name surfaced in app header", "ops-vault" in label, label)

        # ---- create is refused while a vault exists (never orphan it)
        # The workspace name participates in HKDF key derivation, so a nameless
        # create must be refused outright (lock first to return to the gate).
        driver.find_element(By.ID, "btn-lock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
        wsname.clear()
        driver.find_element(By.ID, "pw2").send_keys("another dungeon master key")
        driver.find_element(By.ID, "pw2b").send_keys("another dungeon master key")
        driver.find_element(By.ID, "btn-create").click()
        time.sleep(0.8)
        log = driver.find_element(By.ID, "log").text
        check("create refused with no workspace name", "name" in log.lower(), log)

        # Even a fully-filled create is refused over the live vault (creating
        # again would replace the workspace params and orphan every doc).
        wsname.send_keys("ops-vault")
        driver.find_element(By.ID, "btn-create").click()
        time.sleep(0.8)
        log = driver.find_element(By.ID, "log").text
        check("create refused when a vault already exists",
              "already exists" in log.lower(), log)

        # The refused creates must not have touched the live vault: re-unlock
        # with the real master password and confirm the workspace survives.
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        label = driver.find_element(By.ID, "ws-label").text
        check("refused creates left existing vault intact", "ops-vault" in label, label)

        # ---- create a doc with known plaintext
        # wait until the backend has finished unlocking and the doc list UI
        # is interactive (btn-new is only enabled after setView renders)
        wait_for(driver, EC.visibility_of_element_located((By.ID, "btn-new")), 12)
        driver.find_element(By.ID, "btn-new").click()
        time.sleep(1.0)
        title = driver.find_element(By.ID, "editor-title")
        title.clear()
        title.send_keys(KNOWN_TITLE)
        area = driver.find_element(By.ID, "editor-area")
        area.clear()
        area.send_keys(KNOWN_BODY)
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.5)
        check("document created and saved", "#" not in driver.find_element(By.ID, "log").text)

        # reopen: reload page (simulates closing the browser)
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        check("fresh page shows locked state (AC1 prerequisite)", True)

        # wrong password: still locked
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("definitively-wrong-password")
        driver.find_element(By.ID, "btn-unlock").click()
        time.sleep(1.2)
        log = driver.find_element(By.ID, "log").text
        check("AC3: wrong password denied", "Wrong master password" in log, log)

        # ---- repeated failures stay non-destructive (用户手册 5.1 故障 1: "重复
        # 失败不会损害数据"). Two more consecutive wrong attempts with *different*
        # passwords must each be denied at the verifier and keep the lock screen
        # up; the positive control (the correct password still unlocks the intact
        # vault) is the AC1 check right below.
        for i, wrong in enumerate(["definitively-wrong-password-2",
                                   "definitively-wrong-password-3"], start=2):
            pw.clear()
            pw.send_keys(wrong)
            driver.find_element(By.ID, "btn-unlock").click()
            time.sleep(1.2)
            log = driver.find_element(By.ID, "log").text
            check(f"AC3: repeated wrong password #{i} denied (manual 5.1 故障 1)",
                  "Wrong master password" in log, log)
        check("AC3: repeated failures keep the lock screen up",
              driver.find_element(By.ID, "view-app").get_attribute("style").find("block") == -1,
              driver.find_element(By.ID, "view-app").get_attribute("style"))

        # correct password: opens and shows doc
        pw.clear()
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        # doc rows are rendered after the (async) store reindex completes
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("AC1: doc list shows persisted title after reopen",
              any(KNOWN_TITLE in d.text for d in docs), [d.text for d in docs])

        # ---- edit persists
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        docs[0].click()
        time.sleep(1.0)
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys(KNOWN_EDIT)
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.2)

        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        docs[0].click()
        time.sleep(1.0)
        snippet = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("edit persisted after reopen", KNOWN_EDIT in snippet)

        # ---- Ctrl/Cmd+S shortcut persists (用户手册 3.3: "点击“保存”（或按
        # Ctrl/Cmd + S）"). Drive the document-level keydown handler with a
        # synthesized ctrl+s (the handler is a document keydown listener and
        # does not inspect isTrusted), then prove the save really persisted by
        # reloading and reopening — an AC1-strength proof for the shortcut.
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys("\nCTRL-S-AUTOSAVE-PROOF-77")
        time.sleep(0.3)
        check("typing arms the unsaved-changes guard",
              "edited" in driver.find_element(By.ID, "doc-meta").text.lower())
        driver.execute_script(
            "document.dispatchEvent(new KeyboardEvent('keydown', {key:'s', ctrlKey:true, bubbles:true}))"
        )
        time.sleep(1.2)
        check("ctrl+s shows the saved receipt",
              "Saved" in driver.find_element(By.ID, "log").text)
        check("ctrl+s clears the dirty flag",
              "edited" not in driver.find_element(By.ID, "doc-meta").text.lower())
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")[0].click()
        time.sleep(1.0)
        snippet = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("ctrl+s persisted through reload (AC1 proof for the shortcut)",
              "CTRL-S-AUTOSAVE-PROOF-77" in snippet, snippet[-120:])

        # ---- page-leave reminder mechanism (用户手册 3.3: "未保存的修改在……
        # 离开页面时会收到提醒"). The new-doc/switch-doc prompts are covered
        # above; this pins the browser-level guard: window.onbeforeunload must
        # be armed exactly while the editor is dirty, and disarmed by a save.
        check("clean editor does not arm the page-leave guard",
              driver.execute_script("return window.onbeforeunload === null"))
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys("\nBEFOREUNLOAD-DIRTY")
        time.sleep(0.3)
        check("dirty editor arms the page-leave guard",
              driver.execute_script("return window.onbeforeunload !== null"))
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.2)
        check("save disarms the page-leave guard",
              driver.execute_script("return window.onbeforeunload === null"))

        # ---- P1 editor: markdown preview toggle + XSS safety (real browser)
        # The current doc is open in the editor at this point.
        area = driver.find_element(By.ID, "editor-area")
        # 用户手册 3.5: preview 支持 fenced code (``` / ~~~)、无序与有序列表、
        # 引用（含嵌套层级 ">>"），并自动屏蔽 javascript:、data: 等危险链接。
        area.send_keys(
            '\n## MD Preview Check\n**bold** `code` [bad](javascript:alert(1))\n<script>alert(1)</script>'
            '\n```\nconst x = 1;\n```\n'
            '\n- alpha\n- beta\n'
            '\n1. first\n2. second\n'
            '\n> quote\n>> nested\n'
            '\n[data](data:image/png;base64,AAAA)'
        )
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.6)
        prev = driver.find_element(By.ID, "preview")
        inner = prev.get_attribute("innerHTML")
        check("preview renders markdown headings", "<h2>MD Preview Check</h2>" in inner, inner[:200])
        check("preview renders inline formatting", "<strong>bold</strong>" in inner)
        check("preview escapes raw HTML inert", "<script>alert" not in inner and "&lt;script&gt;" in inner)
        check("preview rejects javascript: links", "<em>bad</em>" in inner and "javascript:" not in inner)
        check("preview renders fenced code block",
              "<pre><code>const x = 1;</code></pre>" in inner, inner[:200])
        check("preview renders unordered list",
              "<ul><li>alpha</li><li>beta</li></ul>" in inner)
        check("preview renders ordered list",
              "<ol><li>first</li><li>second</li></ol>" in inner)
        check("preview renders quote with nested depth",
              "<blockquote>quote</blockquote>" in inner
              and "<blockquote>&gt; nested</blockquote>" in inner, inner[:300])
        check("preview masks data: links as inert text",
              "<em>data</em>" in inner and "data:image/png" not in inner, inner[:300])
        check("preview toggle button flips to Edit", driver.find_element(By.ID, "btn-preview").text == "Edit")
        driver.find_element(By.ID, "btn-preview").click()  # back to editor
        time.sleep(0.4)
        check("preview toggle returns to editor",
              driver.find_element(By.ID, "editor-area").is_displayed())

        # ---- P1 editor: starting a new doc prompts when edits are unsaved
        # (用户手册 3.3: "未保存的修改在切换文档或离开页面时会收到提醒"). The
        # editor above still holds an unsaved edit (the MD-preview text), so
        # clicking New doc must confirm — dismissing keeps the document.
        driver.find_element(By.ID, "btn-new").click()
        time.sleep(0.6)
        alert = driver.switch_to.alert
        check("new doc prompts to discard unsaved edits", True)
        alert.dismiss()
        time.sleep(0.3)
        title_val = driver.find_element(By.ID, "editor-title").get_attribute("value")
        area_val = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("new-doc prompt: dismissing keeps the unsaved document",
              KNOWN_TITLE in title_val and "MD Preview Check" in area_val,
              (title_val, area_val[-120:]))
        check("new-doc prompt: no throwaway doc was created",
              not any("Untitled" in (r.text or "") for r in
                      driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")))

        # ---- table blocks: pipe rows round-trip editor -> encrypted store
        area = driver.find_element(By.ID, "editor-area")
        # Row 4 uses an escaped pipe inside a cell (用户手册 5.1 故障 7):
        # `\|` must stay in-cell and round-trip the encrypted store.
        area.send_keys("\n\n| city | country |\n| --- | --- |\n| Kyoto | Japan |\n| Osaka \\| OSK | Japan |")
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.2)
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.6)
        prev = driver.find_element(By.ID, "preview")
        inner = prev.get_attribute("innerHTML")
        check("table block previews as <table>",
              "<table>" in inner and "<th>city</th>" in inner and "<td>Kyoto</td>" in inner,
              inner[:200])
        check("table block escaped pipe stays one cell",
              "<td>Osaka | OSK</td>" in inner and "\\|" not in inner and "<td>Osaka \\</td>" not in inner,
              inner[:300])
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.4)
        # reload the doc: blocks -> markdown must keep the pipe rows after the
        # save/encrypt/decrypt round-trip through the store.
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")[0].click()
        time.sleep(1.0)
        snippet = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("table round-trips from encrypted store",
              "| Kyoto | Japan |" in snippet, snippet[-300:])
        check("table escaped pipe round-trips from encrypted store",
              "Osaka \\| OSK" in snippet, snippet[-300:])

        # ---- heading levels #..######, nested quotes >/>>, ~~~ fences all
        # round-trip losslessly — 用户手册 3.5: "标题的级别（## 至 ######）
        # 与嵌套引用（>>）同样无损保留"; 编辑器支持 代码块（``` 或 ~~~)。
        # The table round-trip above proves verbatim persistence, but its
        # marker lines only pin "##" and a backtick fence — so drive every
        # heading level, a nested blockquote pair, and the tilde fence through
        # save -> encrypt -> decrypt -> reload back into the editor, then
        # preview them all from the store-derived plaintext.
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys(
            "\n# H1 level\n## H2 level\n### H3 level\n#### H4 level\n"
            "##### H5 level\n###### H6 level\n"
            "> outer level\n>> inner level\n"
            "~~~\nprint('tilde fence')\n~~~"
        )
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.2)
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")[0].click()
        time.sleep(1.0)
        snippet = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("heading levels #..###### round-trip from encrypted store",
              all(f"{'#'*n} H{n} level" in snippet for n in range(1, 7)),
              snippet[-400:])
        check("tilde fence round-trips from encrypted store",
              "~~~\nprint('tilde fence')\n~~~" in snippet, snippet[-200:])
        check("nested quote >> round-trips from encrypted store",
              "> outer level\n>> inner level" in snippet, snippet[-300:])
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.6)
        inner = driver.find_element(By.ID, "preview").get_attribute("innerHTML")
        check("preview renders every heading level 1..6",
              all(f"<h{n}>H{n} level</h{n}>" in inner for n in range(1, 7)),
              inner[-400:])
        check("preview renders the nested quotes as blockquotes",
              "<blockquote>outer level</blockquote>" in inner
              and "&gt; inner level" in inner, inner[-400:])
        check("preview renders the tilde fence as a code block",
              "~~~" not in inner and "tilde fence" in inner, inner[-400:])
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.4)

        # ---- AC2: IndexedDB holds no plaintext
        dump = flatten_dump(get_db_dump(driver))
        for needle in [KNOWN_TITLE, "oscar-7", "48.8584N", "deflector", "Project Snowstorm", "Kyoto", "Osaka"]:
            check(f"AC2: no plaintext ('{needle}') in IndexedDB", needle not in dump)
        docs_json = driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            const r = indexedDB.open('local-kb');
            const db = await new Promise((res) => {
              r.onsuccess = () => res(r.result);
              r.onerror = () => res(null);
            });
            if (!db) return cb('[]');
            const tx = db.transaction('docs', 'readonly');
            const q = tx.objectStore('docs').getAll();
            q.onsuccess = () => { db.close(); cb(JSON.stringify(q.result)); };
            q.onerror = () => { db.close(); cb('[]'); };
          })().catch((e) => cb('[]'));
        """)
        check("AC2: IndexedDB contains ciphertext records", "dekData" in docs_json)

        # ---- AC5: FTS search finds the doc
        search = driver.find_element(By.ID, "search-term")
        search.send_keys("oscar")
        # 用户手册 3.4: "实时列出命中文档" — typing alone (no Go click) must
        # already narrow the list after the input debounce fires.
        time.sleep(1.5)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("AC5: typing in the box searches live (no Go click)",
              any(KNOWN_TITLE in (r.text or "") for r in rows), [r.text for r in rows])
        search.clear()
        time.sleep(1.0)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("AC5: clearing the box restores the full doc list",
              any(KNOWN_TITLE in (r.text or "") for r in rows), [r.text for r in rows])
        search.send_keys("oscar")
        driver.find_element(By.ID, "btn-search").click()
        time.sleep(1.5)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("AC5: search hits the doc by keyword",
              any(KNOWN_TITLE in (r.text or "") for r in rows), [r.text for r in rows])

        # ---- AC5: 用户手册 3.4 "系统对标题、正文、代码、引用与表格单元格进行
        # 全文匹配" — the FTS index covers every block type (built from the
        # store's plaintextForIndex: title + each block's text). Each query is
        # issued against #search-term directly (250ms debounce + Go click),
        # with a clear between queries so hits belong to the current term.
        def search_hits(term):
            search.clear()
            search.send_keys(term)
            driver.find_element(By.ID, "btn-search").click()
            time.sleep(1.2)
            return [r.text or "" for r in
                    driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")]

        rows = search_hits("const")
        check("AC5: search matches fenced code bodies",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        rows = search_hits("nested")
        check("AC5: search matches quote bodies",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        rows = search_hits("Kyoto")
        check("AC5: search matches table cells",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        # 用户手册 3.4 技巧: "关键词可连续用空格分隔" — FTS5 treats the two
        # terms as an implicit AND, so only a doc holding both matches.
        rows = search_hits("Kyoto Japan")
        check("AC5: space-separated keywords AND-match (table cells)",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        rows = search_hits("const oscar")
        check("AC5: space-separated keywords AND-match (code + body)",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        rows = search_hits("zanzibarbazooka")
        check("AC5: search returns nothing for an absent token",
              not any(KNOWN_TITLE in (t or "") for t in rows), rows)
        # 用户手册 5.1 故障 4: "检查检索词是否包含超出分词规则的符号" — a
        # hyphenated token must still hit (the body holds "access code oscar-7"),
        # and a punctuation-only query must degrade to an empty list rather than
        # break the page (unit-level FTS-hardening pins the same on SQLite/FTS5:
        # "a valid query still works afterwards"). Search must stay functional.
        rows = search_hits("oscar-7")
        check("AC5: a hyphenated token (oscar-7) still matches",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        rows = search_hits('"-*(')
        check("AC5: a punctuation-only query yields no hits without crashing",
              not any(KNOWN_TITLE in (t or "") for t in rows), rows)
        rows = search_hits("oscar")
        check("AC5: search still works after a punctuation-only query",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        # Restore the full list deterministically: an empty-query Go click runs
        # doSearch -> refreshDocList (the debounced input handler alone leaves
        # the previous "No matches." list in place until it fires). The import
        # refusal checks below depend on the doc being visible in #doc-list.
        search.clear()
        driver.find_element(By.ID, "btn-search").click()
        time.sleep(1.0)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("AC5: clearing the box after a no-match restores the doc list",
              any(KNOWN_TITLE in (t.text or "") for t in rows), rows)

        # ---- P1 doc switching: clicking another existing doc while the current
        # one has unsaved edits must confirm (用户手册 3.3: "未保存的修改在切换
        # 文档或离开页面时会收到提醒"). The new-doc guard pins the "new doc"
        # path; this pins the third path — the doc-list row click. By now the
        # table/heading/quote/fence content is persisted (the blocks above
        # saved it), so the throwaway switch-target doc only needs a clean
        # create; its raw removal below restores the exactly-one-doc vault all
        # downstream sections (AC4, import, M3 sync, driver2 restore) assume.
        # By now every edit above is persisted, so the editor is clean. Type
        # into the main doc to mark it dirty — the new-doc guard (pinned by the
        # block above) must still fire, and accepting discards only this
        # throwaway marker.
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys("\nSWITCH-LEADIN-MARKER")
        time.sleep(0.3)
        driver.find_element(By.ID, "btn-new").click()
        time.sleep(0.6)
        alert = driver.switch_to.alert
        check("switch-doc: new-doc prompt still active on the dirty editor", True)
        alert.accept()  # create the throwaway doc to switch to
        time.sleep(1.0)
        wait_for(driver, EC.visibility_of_element_located((By.ID, "btn-save")), 12)
        target_title = "SWITCH-TARGET-DOC"
        title = driver.find_element(By.ID, "editor-title")
        title.clear()
        title.send_keys(target_title)
        area = driver.find_element(By.ID, "editor-area")
        area.clear()
        area.send_keys("throwaway switch target body")
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.2)

        def row_by_text(text):
            for r in driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row"):
                if (r.text or "").strip() == text:
                    return r
            return None

        main_row = row_by_text(KNOWN_TITLE)
        check("switch-doc: main doc row present alongside the target",
              main_row is not None and row_by_text(target_title) is not None,
              [r.text for r in driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")])
        main_row.click()  # clean switch: editor was just saved, not dirty
        time.sleep(1.0)
        check("switch-doc: clean switch opens the main doc",
              KNOWN_TITLE in driver.find_element(By.ID, "editor-title").get_attribute("value"))

        # Make the main doc dirty, then try to switch to the target doc: the
        # confirm must appear; dismissing stays on the main doc with the edits.
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys("\nSWITCH-DIRTY-PROOF")
        time.sleep(0.3)
        check("switch-doc: typing marks the doc dirty",
              "edited" in driver.find_element(By.ID, "doc-meta").text.lower())
        row_by_text(target_title).click()
        time.sleep(0.6)
        alert = driver.switch_to.alert
        try:
            alert_text = alert.text or ""
        except Exception:
            alert_text = ""
        check("switching docs prompts to discard unsaved edits (manual 3.3 pin)",
              "Discard unsaved changes" in alert_text, alert_text)
        alert.dismiss()
        time.sleep(0.3)
        title_val = driver.find_element(By.ID, "editor-title").get_attribute("value")
        area_val = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("switch-doc: dismissing stays on the main doc with edits",
              KNOWN_TITLE in title_val and "SWITCH-DIRTY-PROOF" in area_val,
              (title_val, area_val[-120:]))

        # Accepting the discard really switches documents.
        row_by_text(target_title).click()
        time.sleep(0.6)
        alert = driver.switch_to.alert
        alert.accept()
        time.sleep(1.0)
        check("switch-doc: accepting switches to the target doc",
              target_title in driver.find_element(By.ID, "editor-title").get_attribute("value"))

        # The throwaway doc must not survive this block: downstream checks
        # depend on an exactly-one-doc vault (M3 sync receipt "0 docs synced
        # in, 1 skipped" and the final delete leaving zero rows). Remove it via
        # raw IndexedDB (deleteDoc, no tombstone) — a UI delete would write a
        # tombstone into the meta store that then rides the sync tape.
        cleanup = driver.execute_async_script("""
            const cb = arguments[arguments.length - 1];
            (async () => {
              const { IndexedDBBackend } = await import('/idb.js');
              const { openKb } = await import('/src/factory.js');
              const db = new IndexedDBBackend();
              const r = await openKb(db, 'correct horse battery staple');
              if (!r.ok) return cb({ error: 'unlock failed', titles: [] });
              const docs = await r.store.list();
              const hit = docs.find((d) => d.title === 'SWITCH-TARGET-DOC');
              if (hit) {
                await db.deleteDoc(hit.id);
                await db.delMeta('tombstone:' + hit.id);
              }
              return cb({ removed: hit ? hit.id : null,
                          titles: docs.map((d) => d.title) });
            })().catch((e) => cb({ error: String(e), titles: [] }));
        """)
        check("switch-doc: throwaway doc was pruned",
              cleanup.get("removed") is not None, cleanup)
        # Reload so the doc list and FTS index rebuild from the pruned store,
        # then prove the vault is back to exactly the one main doc.
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("switch-doc: vault is back to exactly one doc",
              len(rows) == 1 and KNOWN_TITLE in (rows[0].text or ""),
              [r.text for r in rows])
        rows[0].click()
        time.sleep(1.0)
        check("switch-doc: unsaved edit was never persisted",
              "SWITCH-DIRTY-PROOF" not in driver.find_element(By.ID, "editor-area").get_attribute("value"))

        # ---- AC4: .kb export - download and inspect (ciphertext only)
        # Read the export payload straight from page JS (same serialization as
        # the browser download; avoids post-launch download-dir configuration).
        export_json = driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            const { IndexedDBBackend } = await import('/idb.js');
            const { exportKB } = await import('/src/bak.js');
            const { openKb } = await import('/src/factory.js');
            const db = new IndexedDBBackend();
            const r = await openKb(db, 'correct horse battery staple');
            if (!r.ok) return cb('{"error":"unlock"}');
            const rows = [];
            for (const {id} of await db.listDocs()) rows.push(await db.getDoc(id));
            cb(JSON.stringify(exportKB(await db.getParams(), rows)));
          })().catch((e) => cb(JSON.stringify({ error: String(e) })));
        """)
        export_obj = json.loads(export_json)
        check("AC4: .kb export contains a workspace", export_obj.get("format") == 1)
        check("AC4: .kb export is ciphertext (no plaintext)",
              all(needle not in export_json for needle in
                  [KNOWN_TITLE, "oscar-7", "48.8584N", "Project Snowstorm"]))

        # ---- AC4: .kb carries the crypto parameters claimed by 用户手册 4.1
        # (PBKDF2 迭代次数 310,000; AES-256-GCM 12B IV) and 4.2 (每文档 DEK 由
        # 工作区密钥 AES-256-GCM 封装后与文档密文同存). The workspace params are
        # intentionally cleartext (public nonces/strength), never the password.
        def _b64_bytes(s):
            import base64 as _b64
            pad = "=" * (-len(s) % 4)
            return _b64.urlsafe_b64decode(s + pad)

        ws = export_obj.get("workspace") or {}
        check("AC4: workspaces store PBKDF2 iterations = 310000",
              ws.get("iterations") == 310000, ws.get("iterations"))
        check("AC4: workspace salt is 16 random bytes",
              len(_b64_bytes(ws.get("salt", ""))) == 16, ws.get("salt"))
        ver = ws.get("verifier") or {}
        check("AC4: unlock verifier iv is a 12-byte AES-GCM IV",
              len(_b64_bytes(ver.get("iv", ""))) == 12, ver.get("iv"))
        for d in export_obj.get("documents", []):
            check(f"AC4: doc {str(d.get('id'))[:8]} has a wrapped DEK (dekIv)",
                  len(_b64_bytes(d.get("dekIv", ""))) == 12, d.get("dekIv"))
            check(f"AC4: doc {str(d.get('id'))[:8]} ciphertext iv is 12 bytes",
                  len(_b64_bytes(d.get("iv", ""))) == 12 and d.get("data"), d.get("iv"))
        check("AC4: deleted docs do not reappear as .kb documents",
              all(not str(d.get("id", "")).startswith("tombstone:")
                  for d in export_obj.get("documents", [])))

        # ---- AC4: the real Export .kb button downloads a timestamp-stamped
        # filename (用户手册 3.6: "浏览器下载仅含密文的备份文件（文件名含导出
        # 时间）"). Capturing the click with a patched anchor avoids writing a
        # real download to disk; the value under test is stampedName()'s
        # `local-kb-backup-<UTC YYYY-MM-DD HH MM SS>.kb` pattern, which the
        # restored file needs to be distinguishable from other backups.
        before = datetime.now(timezone.utc).replace(microsecond=0)
        captured = driver.execute_script("""
          window.__captured = null;
          const orig = HTMLAnchorElement.prototype.click;
          HTMLAnchorElement.prototype.click = function () {
            if (this.download) window.__captured = this.download;
            orig.call(this);
          };
          document.getElementById('btn-export').click();
          return true;
        """)
        name = driver.execute_script("return window.__captured")
        after = datetime.now(timezone.utc).replace(microsecond=0)
        time.sleep(0.5)
        stamp_re = r"^local-kb-backup-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.kb$"
        check("AC4: .kb export filename embeds the export timestamp",
              bool(re.match(stamp_re, name or "")), name)
        if name:
            stamp = re.match(stamp_re, name).group(0) \
                .replace("local-kb-backup-", "").replace(".kb", "")
            ts = datetime.strptime(stamp, "%Y-%m-%d-%H-%M-%S").replace(tzinfo=timezone.utc)
            check("AC4: .kb export filename timestamp is current (within the run window)",
                  before <= ts <= after, f"ts={ts} before={before} after={after}")

        # ---- unlocked-state .kb import refusal (用户手册 3.7 场景二 / 5.1
        # 故障 3): importing a foreign-workspace or malformed backup while in
        # the app must refuse loudly in the log instead of surfacing as an
        # unhandled rejection with no message — and must leave the vault intact.
        foreign = json.loads(export_json)
        # A different workspace salt → different derived workspace key.
        foreign["workspace"]["salt"] = "AAAAAAAAAAAAAAAAAAAAAA"
        foreign_bytes = json.dumps(foreign).encode("utf-8")
        driver.execute_script("""
          const f = new File([new Uint8Array(arguments[0])], 'foreign.kb',
                             { type: 'application/json' });
          const dt = new DataTransfer();
          dt.items.add(f);
          const inp = document.getElementById('import-file');
          inp.files = dt.files;
          inp.dispatchEvent(new Event('change', { bubbles: true }));
        """, list(foreign_bytes))
        wait_for(driver, EC.alert_is_present(), 6)
        driver.switch_to.alert.accept()  # confirm; the salt gate then refuses
        time.sleep(1.2)
        log = driver.find_element(By.ID, "log").text
        check("import: foreign-workspace .kb refused with a message",
              "Import refused" in log and "different workspace" in log, log)
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("import: refused .kb left the vault untouched",
              any(KNOWN_TITLE in (d.text or "") for d in docs),
              [d.text for d in docs])
        # malformed .kb (valid local-kb envelope, missing workspace) must be
        # refused at importKB, before any write, with a surfaced message
        broken_bytes = json.dumps({"app": "local-kb", "format": 1}).encode("utf-8")
        driver.execute_script("""
          const f = new File([new Uint8Array(arguments[0])], 'broken.kb',
                             { type: 'application/json' });
          const dt = new DataTransfer();
          dt.items.add(f);
          const inp = document.getElementById('import-file');
          inp.files = dt.files;
          inp.dispatchEvent(new Event('change', { bubbles: true }));
        """, list(broken_bytes))
        wait_for(driver, EC.alert_is_present(), 6)
        driver.switch_to.alert.accept()  # confirm; the shape check then refuses
        time.sleep(1.2)
        log = driver.find_element(By.ID, "log").text
        check("import: malformed .kb refused with a message",
              "Import refused" in log and "broken" in log, log)

        # ---- unlocked-state .kb import merge (用户手册 3.7 场景二): re-importing
        # a same-workspace backup in the app must merge idempotently — confirm
        # asked, log count matches the payload, existing ids skipped (幂等导入),
        # no duplicate rows. This is the positive branch of the two refusal
        # tests above, driven through the same real #import-file path.
        same_kb_bytes = export_json.encode("utf-8")
        driver.execute_script("""
          const f = new File([new Uint8Array(arguments[0])], 'same.kb',
                             { type: 'application/json' });
          const dt = new DataTransfer();
          dt.items.add(f);
          const inp = document.getElementById('import-file');
          inp.files = dt.files;
          inp.dispatchEvent(new Event('change', { bubbles: true }));
        """, list(same_kb_bytes))
        wait_for(driver, EC.alert_is_present(), 6)
        driver.switch_to.alert.accept()  # confirm the merge
        time.sleep(1.2)
        log = driver.find_element(By.ID, "log").text
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("import: same-workspace .kb merge logged the payload count",
              "Imported 1 docs." in log, log)
        check("import: merged vault still holds exactly the original doc",
              len(docs) == 1 and KNOWN_TITLE in (docs[0].text or ""),
              [d.text for d in docs])
        # second identical import: same-salt backup, existing id → duplicate
        # skip (importKB 幂等导入) — the vault must not grow a second row
        driver.execute_script("""
          const f = new File([new Uint8Array(arguments[0])], 'same2.kb',
                             { type: 'application/json' });
          const dt = new DataTransfer();
          dt.items.add(f);
          const inp = document.getElementById('import-file');
          inp.files = dt.files;
          inp.dispatchEvent(new Event('change', { bubbles: true }));
        """, list(same_kb_bytes))
        wait_for(driver, EC.alert_is_present(), 6)
        driver.switch_to.alert.accept()
        time.sleep(1.2)
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("import: re-import of the same backup is idempotent (no duplicates)",
              len(docs) == 1 and KNOWN_TITLE in (docs[0].text or ""),
              [d.text for d in docs])

        # ---- M3 sync: build a .kbsync tape, apply it to a fresh peer, verify
        # the peer converges and the carried tape is ciphertext-only.
        sync_res = driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            const { IndexedDBBackend } = await import('/idb.js');
            const { openKb } = await import('/src/factory.js');
            const { buildTape, serializeTape, applyTapeFile } = await import('/src/syncfile.js');
            const db = new IndexedDBBackend();
            const r = await openKb(db, 'correct horse battery staple');
            if (!r.ok) return cb({ error: 'unlock' });
            const tape = await buildTape(r.params, db);
            const txt = new TextDecoder().decode(serializeTape(tape));
            // Fresh peer sharing the same workspace params.
            const peer = new IndexedDBBackend('local-kb-peer');
            await peer.saveParams(r.params);
            const pr = await openKb(peer, 'correct horse battery staple');
            if (!pr.ok) return cb({ error: 'peer-unlock' });
            const res = await applyTapeFile(peer, tape);
            const ids = (await peer.listDocs()).map((d) => d.id);
            return cb({ tapeJson: txt, applied: res.applied, peerDocs: ids.length });
          })().catch((e) => cb({ error: String(e) }));
        """)
        sync_obj = json.loads(json.dumps(sync_res))
        check("M3: peer converged via sync tape", sync_obj.get("applied", -1) > 0 and sync_obj.get("peerDocs", 0) > 0)

        tape_json = sync_obj.get("tapeJson") or ""
        check("M3: sync tape is ciphertext (no plaintext)",
              all(x not in tape_json for x in [KNOWN_TITLE, "oscar-7", "48.8584N", "Project Snowstorm"]))
        ids_json = json.dumps(sync_obj.get("peerDocs", 0))
        check("M3: sync tape carried a doc (peer lists " + ids_json + ")", sync_obj.get("peerDocs", 0) >= 1)

        # ---- 用户手册 3.8 real-button sync flow: the app's Sync out button must
        # download a timestamp-stamped .kbsync (同 .kb 的 stampedName 命名规则),
        # the Sync in file input must apply a same-workspace tape with the app's
        # own "N docs synced in, M skipped." receipt (LWW idempotence: this vault
        # already holds that exact version → skipped, vault unchanged), and a
        # foreign-workspace tape must be refused by the workspace gate in the log
        # ("Sync refused: ... different workspace") with the vault untouched.
        # The export block above installed the anchor click hook, so the real
        # button click is captured into window.__captured without a disk write.
        sync_before = datetime.now(timezone.utc).replace(microsecond=0)
        driver.execute_script("""
          window.__captured = null;
          document.getElementById('btn-sync-out').click();
          return true;
        """)
        sync_name = driver.execute_script("return window.__captured")
        sync_after = datetime.now(timezone.utc).replace(microsecond=0)
        time.sleep(0.5)
        sync_stamp_re = r"^local-kb-sync-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.kbsync$"
        check("sync: Sync out downloads a timestamp-stamped .kbsync name",
              bool(re.match(sync_stamp_re, sync_name or "")), sync_name)
        if sync_name:
            sync_stamp = re.match(sync_stamp_re, sync_name).group(0) \
                .replace("local-kb-sync-", "").replace(".kbsync", "")
            sync_ts = datetime.strptime(sync_stamp, "%Y-%m-%d-%H-%M-%S").replace(tzinfo=timezone.utc)
            check("sync: .kbsync filename timestamp is current (within the run window)",
                  sync_before <= sync_ts <= sync_after,
                  f"ts={sync_ts} before={sync_before} after={sync_after}")
        log = driver.find_element(By.ID, "log").text
        check("sync: Sync out logs the ciphertext-only tape receipt",
              "Wrote sync tape" in log and "ciphertext only" in log, log)

        # Reuse the app-level Sync in input with a tape freshly built from THIS
        # vault: same workspace, same exact updatedAt → the LWW no-op branch
        # (skipped) — the receipt must read "0 docs synced in, 1 skipped." and
        # the doc list must stay at exactly one doc (the delete block depends
        # on that count).
        fresh_tape = driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            const { IndexedDBBackend } = await import('/idb.js');
            const { openKb } = await import('/src/factory.js');
            const { buildTape, serializeTape } = await import('/src/syncfile.js');
            const db = new IndexedDBBackend();
            const r = await openKb(db, 'correct horse battery staple');
            if (!r.ok) return cb('{"error":"unlock"}');
            const tape = await buildTape(r.params, db);
            cb(new TextDecoder().decode(serializeTape(tape)));
          })().catch((e) => cb('{"error":"' + String(e) + '"}'));
        """)
        fresh_bytes = fresh_tape.encode("utf-8")
        driver.execute_script("""
          const f = new File([new Uint8Array(arguments[0])], 'same.kbsync',
                             { type: 'application/json' });
          const dt = new DataTransfer();
          dt.items.add(f);
          const inp = document.getElementById('sync-file');
          inp.files = dt.files;
          inp.dispatchEvent(new Event('change', { bubbles: true }));
        """, list(fresh_bytes))
        time.sleep(2.0)  # apply + openKb re-derive (PBKDF2) + refreshDocList
        log = driver.find_element(By.ID, "log").text
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("sync: same-workspace Sync in logs the skip receipt",
              "0 docs synced in, 1 skipped." in log, log)
        check("sync: same-workspace Sync in left the vault at exactly one doc",
              len(docs) == 1 and KNOWN_TITLE in (docs[0].text or ""),
              [d.text for d in docs])

        # A .kbsync from a different workspace (tampered salt → different derived
        # workspace key) must be refused at the workspace gate without touching
        # the vault — the "Sync refused" message is the app-level error channel.
        foreign_tape = json.loads(fresh_tape)
        foreign_tape["workspace"]["salt"] = "AAAAAAAAAAAAAAAAAAAAAA"
        foreign_bytes = json.dumps(foreign_tape).encode("utf-8")
        driver.execute_script("""
          const f = new File([new Uint8Array(arguments[0])], 'foreign.kbsync',
                             { type: 'application/json' });
          const dt = new DataTransfer();
          dt.items.add(f);
          const inp = document.getElementById('sync-file');
          inp.files = dt.files;
          inp.dispatchEvent(new Event('change', { bubbles: true }));
        """, list(foreign_bytes))
        time.sleep(1.2)
        log = driver.find_element(By.ID, "log").text
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("sync: foreign-workspace tape refused by the workspace gate",
              "Sync refused" in log and "different workspace" in log, log)
        check("sync: refused tape left the vault untouched",
              len(docs) == 1 and KNOWN_TITLE in (docs[0].text or ""),
              [d.text for d in docs])

        # ---- lock-screen .kb restore onto a fresh machine (用户手册 3.7,
        # 登记表 功能4, SPEC AC4): a brand-new browser profile has no vault to
        # unlock and no way to reach the in-app Import .kb (view-app only), so
        # the lock screen must accept the exported backup directly, create the
        # vault from the backup's params and unlock with the backup password.
        # A second headless profile acts as the fresh machine; the first
        # driver's IndexedDB must stay untouched.
        kb_bytes = export_json.encode("utf-8")
        # A second geckodriver session gets its own isolated temp profile
        # (same launch pattern as the main driver), so this browser has no
        # vault at all — the fresh-machine premise of the restore path.
        opts2 = Options()
        opts2.headless = True
        opts2.binary_location = fb
        opts2.set_preference("dom.indexedDB.enabled", True)
        driver2 = webdriver.Firefox(options=opts2)
        try:
            driver2.get(BASE + "/")
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            check("restore: fresh profile shows the lock screen", True)
            # A malformed envelope that still passes deserializeKB (app id set but
            # no workspace / no documents) must be refused with a message, not
            # crash restoreKBFile on kb.workspace.salt / kb.documents.length —
            # and must leave the fresh profile vault-less for the real restore.
            broken_bytes = json.dumps({"app": "local-kb", "format": 1}).encode("utf-8")
            driver2.execute_script("""
              const f = new File([new Uint8Array(arguments[0])], 'broken.kb',
                                 { type: 'application/json' });
              const dt = new DataTransfer();
              dt.items.add(f);
              const inp = document.getElementById('restore-file');
              inp.files = dt.files;
              inp.dispatchEvent(new Event('change', { bubbles: true }));
            """, list(broken_bytes))
            driver2.find_element(By.ID, "restore-pw").send_keys("correct horse battery staple")
            driver2.find_element(By.ID, "btn-restore").click()
            time.sleep(1.2)
            log2 = driver2.find_element(By.ID, "log").text
            check("restore: malformed .kb refused cleanly on the lock screen",
                  "Restore refused" in log2 and "broken" in log2, log2)
            check("restore: refused .kb left the fresh profile locked",
                  driver2.find_element(By.ID, "view-unlock").is_displayed())
            # hand the .kb backup to the hidden file input the same way a real
            # <input type=file> change event does
            driver2.execute_script("""
              const f = new File([new Uint8Array(arguments[0])], 'backup.kb',
                                 { type: 'application/json' });
              const dt = new DataTransfer();
              dt.items.add(f);
              const inp = document.getElementById('restore-file');
              inp.files = dt.files;
              inp.dispatchEvent(new Event('change', { bubbles: true }));
            """, list(kb_bytes))
            rpw = driver2.find_element(By.ID, "restore-pw")
            rpw.clear()  # the broken attempt above already typed a password
            rpw.send_keys("correct horse battery staple")
            driver2.find_element(By.ID, "btn-restore").click()
            wait_for(driver2, EC.alert_is_present(), 6)
            driver2.switch_to.alert.accept()  # confirm: create the vault from the backup
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-app")), 15)
            wait_for(driver2, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
            rows = driver2.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("restore: backup doc appears in the fresh vault",
                  any(KNOWN_TITLE in (r.text or "") for r in rows), [r.text for r in rows])
            log2 = driver2.find_element(By.ID, "log").text
            check("restore: log confirms the restore by document count",
                  "Restored .kb backup" in log2, log2)

            # ---- lock-screen restore refusal for a *different* workspace
            # (用户手册 3.7 场景一: "若当前浏览器已存在同一工作区（盐值相同）则会先
            # 确认再合并导入；若已存在其他工作区（盐值不同）则恢复会被拒绝" and
            # 5.1 故障 3). driver2 now holds the restored (same-salt) vault;
            # refresh it back to the lock screen, hand it a foreign-salt .kb,
            # and the lock-screen precheck (main.js restoreKBFile) must refuse
            # BEFORE any confirm()/unlock — the existing vault stays restorable.
            driver2.refresh()
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            foreign2 = json.loads(export_json)
            # a different workspace salt → different derived workspace key
            foreign2["workspace"]["salt"] = "AAAAAAAAAAAAAAAAAAAAAA"
            foreign2_bytes = json.dumps(foreign2).encode("utf-8")
            driver2.execute_script("""
              const f = new File([new Uint8Array(arguments[0])], 'foreign-restore.kb',
                                 { type: 'application/json' });
              const dt = new DataTransfer();
              dt.items.add(f);
              const inp = document.getElementById('restore-file');
              inp.files = dt.files;
              inp.dispatchEvent(new Event('change', { bubbles: true }));
            """, list(foreign2_bytes))
            driver2.find_element(By.ID, "restore-pw").send_keys("correct horse battery staple")
            driver2.find_element(By.ID, "btn-restore").click()
            # no confirm() is offered for a foreign salt — the precheck returns
            # before the merge/create-vault paths can arm one
            time.sleep(1.2)
            log2 = driver2.find_element(By.ID, "log").text
            check("restore: foreign-workspace .kb refused on the lock screen (manual 3.7/故障 3)",
                  "Restore refused" in log2 and "different workspace" in log2, log2)
            check("restore: refused restore left the profile on the lock screen",
                  driver2.find_element(By.ID, "view-unlock").is_displayed())
            # the existing same-salt vault is untouched: unlock and the doc returns
            driver2.find_element(By.ID, "pw").send_keys("correct horse battery staple")
            driver2.find_element(By.ID, "btn-unlock").click()
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-app")), 15)
            wait_for(driver2, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
            rows2 = driver2.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("restore: refused restore left the existing vault intact",
                  any(KNOWN_TITLE in (r.text or "") for r in rows2), [r.text for r in rows2])

            # ---- lock-screen restore merge for the *same* workspace
            # (用户手册 3.7 场景一: "若当前浏览器已存在同一工作区（盐值相同）则会先
            # 确认再合并导入" — the positive branch: lock the existing vault back,
            # feed it the *unmodified* backup of the same workspace (byte-identical
            # salt, so the derived workspace key matches), and the lock screen must
            # arm the merge confirm, then merge without duplicating the doc.
            driver2.refresh()
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            driver2.execute_script("""
              const f = new File([new Uint8Array(arguments[0])], 'same-restore.kb',
                                 { type: 'application/json' });
              const dt = new DataTransfer();
              dt.items.add(f);
              const inp = document.getElementById('restore-file');
              inp.files = dt.files;
              inp.dispatchEvent(new Event('change', { bubbles: true }));
            """, list(kb_bytes))
            driver2.find_element(By.ID, "restore-pw").send_keys("correct horse battery staple")
            driver2.find_element(By.ID, "btn-restore").click()
            # a same-salt backup over an existing vault arms the merge confirm —
            # unlike the foreign-salt refusal above, this pauses for the user
            wait_for(driver2, EC.alert_is_present(), 6)
            driver2.switch_to.alert.accept()  # confirm: "…Merge the backup into it?"
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-app")), 15)
            mount_rows = driver2.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            t2 = [r.text for r in mount_rows]
            check("restore: same-salt backup merges at the lock screen confirm (manual 3.7 场景一)",
                  len(mount_rows) == 1 and KNOWN_TITLE in (t2[0] or ""), t2)
            log2 = driver2.find_element(By.ID, "log").text
            check("restore: merged log confirms the restore by document count",
                  "Restored .kb backup" in log2, log2)

            # ---- wrong master password on the lock-screen restore stays
            # non-destructive (用户手册 3.2: "密码错误将被校验器直接拒绝，不会产生
            # 任何可读数据，也不会破坏现有数据"; 5.3 Q1: "主密码无法找回"). The
            # same-salt backup merges fine, but re-unlocking with the *typed*
            # password must fail at the verifier, leaving the profile on the lock
            # screen; the existing vault then still unlocks with the real password.
            driver2.refresh()
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            driver2.execute_script("""
              const f = new File([new Uint8Array(arguments[0])], 'wrongpw-restore.kb',
                                 { type: 'application/json' });
              const dt = new DataTransfer();
              dt.items.add(f);
              const inp = document.getElementById('restore-file');
              inp.files = dt.files;
              inp.dispatchEvent(new Event('change', { bubbles: true }));
            """, list(kb_bytes))
            driver2.find_element(By.ID, "restore-pw").send_keys("definitely the wrong password")
            driver2.find_element(By.ID, "btn-restore").click()
            # same-salt backup over the existing vault -> merge confirm fires
            wait_for(driver2, EC.alert_is_present(), 6)
            driver2.switch_to.alert.accept()  # confirm: "…Merge the backup into it?"
            # importKB merges idempotently, then openKb hits the verifier with the
            # wrong password and fails -> the lock screen must stay up. The failed
            # branch never switches views, so wait on the log message itself (the
            # async restore path has not finished until the refusal line appears).
            wait_for(driver2, lambda d: "not accepted" in (
                d.find_element(By.ID, "log").text or ""), 12)
            log2 = driver2.find_element(By.ID, "log").text
            check("restore: wrong master password is refused after the merge",
                  "Restored the backup, but its master password was not accepted." in log2, log2)
            check("restore: profile stays on the lock screen after failed unlock",
                  driver2.find_element(By.ID, "view-app").get_attribute("style").find("block") == -1,
                  driver2.find_element(By.ID, "view-app").get_attribute("style"))
            # the correct password still unlocks the untouched single-doc vault
            driver2.find_element(By.ID, "pw").send_keys("correct horse battery staple")
            driver2.find_element(By.ID, "btn-unlock").click()
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-app")), 15)
            rows2 = driver2.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            t2 = [r.text for r in rows2]
            check("restore: wrong-password attempt left the vault intact and unlockable (manual 3.2/Q1)",
                  len(rows2) == 1 and KNOWN_TITLE in (t2[0] or ""), t2)

            # ---- a deleted doc cannot be resurrected by an OLD backup or tape
            # (用户手册 3.3: "删除会记录同步墓碑，之后即使从旧备份/旧同步磁带也
            # 无法 复活 该文档"). driver2's vault holds this exact workspace, so
            # the export_json bytes captured earlier ARE the pre-delete .kb of
            # the same doc (same salt, same id), and the tape captured below is
            # the pre-delete .kbsync. Delete the doc, then feed both back — the
            # tombstone must dominate (importKB skips tombstoned ids; applyTape
            # skips live entries dominated by a local tombstone).
            # capture the pre-delete tape first, while the doc is still live
            prekill_tape = driver2.execute_async_script("""
              const cb = arguments[arguments.length - 1];
              (async () => {
                const { IndexedDBBackend } = await import('/idb.js');
                const { openKb } = await import('/src/factory.js');
                const { buildTape, serializeTape } = await import('/src/syncfile.js');
                const db = new IndexedDBBackend();
                const r = await openKb(db, 'correct horse battery staple');
                if (!r.ok) return cb('{"error":"unlock"}');
                const tape = await buildTape(r.params, db);
                cb(new TextDecoder().decode(serializeTape(tape)));
              })().catch((e) => cb('{"error":"' + String(e) + '"}'));
            """)
            driver2.find_element(By.CSS_SELECTOR, "#doc-list .doc-row").click()
            time.sleep(0.8)
            driver2.find_element(By.ID, "btn-delete").click()
            time.sleep(0.6)
            driver2.switch_to.alert.accept()  # confirm the deletion
            time.sleep(1.0)
            rows2 = driver2.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("resurrect: delete confirmed on the restore profile",
                  len(rows2) == 0, [r.text for r in rows2])
            # in-app Import (场景二) of the OLD backup must respect the tombstone
            # that now lives in this vault: the doc's id is skipped on import,
            # so the list stays empty instead of the doc reappearing.
            driver2.execute_script("""
              const f = new File([new Uint8Array(arguments[0])], 'old.kb',
                                 { type: 'application/json' });
              const dt = new DataTransfer();
              dt.items.add(f);
              const inp = document.getElementById('import-file');
              inp.files = dt.files;
              inp.dispatchEvent(new Event('change', { bubbles: true }));
            """, list(kb_bytes))
            wait_for(driver2, EC.alert_is_present(), 8)
            driver2.switch_to.alert.accept()  # confirm: "Import 1 docs?..."
            time.sleep(2.0)
            log2 = driver2.find_element(By.ID, "log").text
            rows2 = driver2.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("resurrect: old .kb import cannot revive the deleted doc (manual 3.3)",
                  len(rows2) == 0, (log2, [r.text for r in rows2]))
            # the old .kbsync tape (same workspace gate) must no-op against the
            # tombstone — a live entry at the deleted doc's timestamp is
            # dominated locally, so the doc stays gone
            driver2.execute_script("""
              const f = new File([new Uint8Array(arguments[0])], 'old.kbsync',
                                 { type: 'application/json' });
              const dt = new DataTransfer();
              dt.items.add(f);
              const inp = document.getElementById('sync-file');
              inp.files = dt.files;
              inp.dispatchEvent(new Event('change', { bubbles: true }));
            """, list(prekill_tape.encode("utf-8")))
            time.sleep(2.0)  # apply + openKb re-derive (PBKDF2) + refreshDocList
            log2 = driver2.find_element(By.ID, "log").text
            rows2 = driver2.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("resurrect: old .kbsync tape cannot revive the deleted doc (manual 3.3)",
                  len(rows2) == 0 and "skipped" in log2,
                  (log2, [r.text for r in rows2]))
        finally:
            driver2.quit()

        # ---- re-key: change master password without losing the vault
        # Save the dirty preview edits first so no unsaved-changes guard fires.
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.2)
        NEW_PW = "brand new master password 7381"
        driver.find_element(By.ID, "rekey-current").send_keys("correct horse battery staple")
        driver.find_element(By.ID, "rekey-new").send_keys(NEW_PW)
        driver.find_element(By.ID, "rekey-confirm").send_keys(NEW_PW)
        driver.find_element(By.ID, "btn-rekey").click()
        time.sleep(2.5)
        log = driver.find_element(By.ID, "log").text
        check("re-key: password changed (log confirms)",
              "Master password changed" in log and "re-wrapped" in log, log)

        # old password must now be refused at the unlock gate
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        time.sleep(1.2)
        log = driver.find_element(By.ID, "log").text
        check("re-key: old master password now denied", "Wrong master password" in log, log)

        # new password unlocks and the doc list still holds the known doc
        pw.clear()
        pw.send_keys(NEW_PW)
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("re-key: new password unlocks vault with docs intact",
              any(KNOWN_TITLE in d.text for d in docs), [d.text for d in docs])

        # ---- Lock: drop the master password from the session and clear memory
        driver.find_element(By.ID, "btn-lock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
        check("lock: returns to the unlock view", True)
        pw_held = driver.execute_script("return sessionStorage.getItem('local-kb-pw');")
        check("lock: master password dropped from session",
              pw_held is None or pw_held == "", str(pw_held))
        editor = driver.find_element(By.ID, "editor-area").get_attribute("value")
        docrows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("lock: editor cleared and doc list emptied",
              editor == "" and len(docrows) == 0, f"editor={editor!r} rows={len(docrows)}")

        # re-unlock with the (new) master password; ciphertext is still on disk
        pw = driver.find_element(By.ID, "pw")
        pw.clear()
        pw.send_keys(NEW_PW)
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("lock: re-unlock shows the docs again (data intact)",
              any(KNOWN_TITLE in d.text for d in docs), [d.text for d in docs])

        # ---- idle auto-lock (manual 3.2: re-lock after a default idle period)
        # Shrink the idle window to real seconds via the documented page-global
        # override, then wait for the timer to fire on its own. Manual 3.2 also
        # promises "自动锁定前会自动保存未保存的编辑，不会丢失内容" — a dirty
        # editor edit must be auto-saved before the key is dropped, so open the
        # doc and type an UNSAVED edit first. The mousemove at the end re-arms
        # the timer after the last keydown; after that, no page interaction may
        # happen before the lock fires, or the test would just postpone it.
        driver.find_element(By.CSS_SELECTOR, "#doc-list .doc-row").click()
        time.sleep(1.0)
        area = driver.find_element(By.ID, "editor-area")
        marker = "\nIDLE-AUTOSAVE-PROOF"
        area.send_keys(marker)
        time.sleep(0.3)  # let the last input event mark the editor dirty
        driver.execute_script("""
          window.LOCAL_KB_IDLE_MS = 3000;
          document.dispatchEvent(new MouseEvent('mousemove'));
        """)
        time.sleep(4.2)
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
        check("idle auto-lock: returns to the unlock gate", True)
        pw_held = driver.execute_script("return sessionStorage.getItem('local-kb-pw');")
        check("idle auto-lock: master password dropped from session",
              pw_held is None or pw_held == "", str(pw_held))
        log = driver.find_element(By.ID, "log").text
        check("idle auto-lock: log reports the inactivity lock",
              "auto-locked" in log.lower(), log)

        # re-unlock: the edit was never saved manually, so its survival proves
        # idleLock's auto-save ran before the session password was dropped
        pw = driver.find_element(By.ID, "pw")
        pw.clear()
        pw.send_keys(NEW_PW)
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        driver.find_element(By.CSS_SELECTOR, "#doc-list .doc-row").click()
        time.sleep(1.0)
        area_val = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("idle auto-lock: unsaved edit auto-saved before the lock (manual 3.2)",
              "IDLE-AUTOSAVE-PROOF" in area_val, area_val[-120:])

        # The 3-second idle window was a test-only shrink for the block above.
        # Restore a long window and re-arm, or every later block would race the
        # auto-lock in its sleeps (the delete flow's modal confirm alone spans
        # several 3-second windows, and timers keep running while an alert is up).
        driver.execute_script("""
          window.LOCAL_KB_IDLE_MS = 3600000;
          document.dispatchEvent(new MouseEvent('mousemove'));
        """)

        # IndexedDB must hold ciphertext only, with no new-password plaintext
        dump = flatten_dump(get_db_dump(driver))
        for needle in [NEW_PW, "correct horse battery staple"]:
            check(f"re-key: no master password plaintext in IndexedDB ('{needle}')", needle not in dump)

        # ---- document delete: confirm gate + tombstone + persistence
        # (用户手册 3.3: "删除文档：在文档列表对目标文档执行删除并确认；删除
        #  会记录同步墓碑" and 5.1 故障 5: "再检查是否误删（删除有二次确认）").
        # The delete flow must (a) do nothing until the confirm is accepted,
        # (b) leave a tombstone in the encrypted meta store, and (c) persist —
        # a reload + re-unlock must not resurrect the document. Pinned here at
        # the end because everything above (AC5 search, AC4 export, sync,
        # restore, re-key) needs this document to still exist.
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        target = next(r for r in rows if KNOWN_TITLE in (r.text or ""))
        target.click()
        time.sleep(1.0)
        driver.find_element(By.ID, "btn-delete").click()
        time.sleep(0.8)
        alert = driver.switch_to.alert
        check("delete: confirm dialog gates the deletion (manual 5.1 故障 5)", True)
        alert.dismiss()
        # the confirm returned false, so nothing may be deleted — but the doc
        # list re-renders around the dialog, so wait for it to settle instead
        # of racing a blind sleep against the recount (observed transiently
        # empty at this spot in cycle 46; the log below proves no deletion).
        wait_for(driver, EC.presence_of_element_located(
            (By.CSS_SELECTOR, "#doc-list .doc-row")), 8)
        time.sleep(0.3)
        log = driver.find_element(By.ID, "log").text
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("delete: dismissing the confirm keeps the document",
              any(KNOWN_TITLE in (r.text or "") for r in rows), [r.text for r in rows])
        check("delete: dismissing the confirm logs no deletion", "Document deleted." not in log, log)

        # accept the confirm: the doc must leave the list, the editor clears,
        # and the log confirms the action (manual 3.3 "删除……并确认")
        wait_for(driver, EC.element_to_be_clickable((By.ID, "btn-delete")), 8)
        driver.find_element(By.ID, "btn-delete").click()
        time.sleep(0.8)
        alert = driver.switch_to.alert
        alert.accept()
        time.sleep(1.2)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("delete: accepting the confirm removes the doc from the list",
              len(rows) == 0, [r.text for r in rows])
        log = driver.find_element(By.ID, "log").text
        check("delete: log confirms the deletion", "Document deleted." in log, log)
        title_val = driver.find_element(By.ID, "editor-title").get_attribute("value")
        check("delete: editor cleared after deletion", title_val == "", title_val)

        # a tombstone (the deleted doc's non-secret updatedAt) is kept in the
        # meta store so the delete propagates through the sync tape
        dump = flatten_dump(get_db_dump(driver))
        check("delete: tombstone recorded in IndexedDB meta (manual 3.3)",
              "tombstone:" in dump, dump[-200:])

        # reload + re-unlock: the delete must have persisted (AC1 proof), the
        # document stays gone instead of being resurrected from the store
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys(NEW_PW)
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list")), 12)
        time.sleep(1.2)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("delete: deleted doc stays absent after reload + re-unlock",
              not any(KNOWN_TITLE in (r.text or "") for r in rows), [r.text for r in rows])
        check("delete: doc list is empty after the only doc was deleted",
              len(rows) == 0, [r.text for r in rows])

        # ---- AC6: offline — assert page JS/import loads (no network fallback)
        # The page itself is a static module graph served from /public; language
        # server already covered by reloads above. Also verify demo serves offline
        # static files with no external CDN references.
        html = driver.page_source
        check("AC6: page references no external network",
              all(x not in html for x in ["https://", "//cdn", "http://"]))

        # ---- 用户手册 4.1 (服务端口 4242，可用环境变量 PORT 覆盖), 5.1 故障 6
        # 处理 ("设置环境变量 PORT=其他端口后重启") and Q2 ("演示服务器仅提供
        # 静态文件传输服务"): pin the demo server's port + offline contract on
        # the wire — the /api/health offline marker, the default port serving
        # index.html, and a second instance started with PORT=<alt> answering on
        # that port. Stdlib http.client/subprocess only, no new dependencies.
        import http.client as _hc, socket as _sk, subprocess as _sp
        _host = BASE.split("://", 1)[1].rsplit(":", 1)[0]
        _port = int(BASE.split("://", 1)[1].rsplit(":", 1)[1])

        conn = _hc.HTTPConnection(_host, _port, timeout=5)
        conn.request("GET", "/api/health")
        hr = conn.getresponse()
        hbody = json.loads(hr.read().decode("utf-8"))
        conn.close()
        check("server: /api/health reports the offline static server (manual Q2)",
              hr.status == 200 and hbody.get("ok") is True
              and hbody.get("offline") is True and hbody.get("app") == "local-kb",
              f"status={hr.status} {hbody}")

        conn = _hc.HTTPConnection(_host, _port, timeout=5)
        conn.request("GET", "/")
        pr = conn.getresponse()
        pbody = pr.read().decode("utf-8", "replace")
        conn.close()
        check("server: default port serves the app's index.html (manual 2.1/4.1)",
              pr.status == 200 and "view-unlock" in pbody and "Local KB" in pbody,
              f"status={pr.status} bytes={len(pbody)}")

        # 用户手册 4.1 可配置项: "自动锁定 闲置 5 分钟" — the shipped default
        # must stay exactly 5 * 60 * 1000 ms. The idle-lock behavior above is
        # E2E-pinned through the window.LOCAL_KB_IDLE_MS override, so the real
        # default value itself is only attested here, at the served source.
        import urllib.request as _ur
        with _ur.urlopen(f"http://{_host}:{_port}/main.js", timeout=8) as _r:
            mjs = _r.read().decode("utf-8", "replace")
        _idle = re.search(r"IDLE_LOCK_MS\s*=\s*(\d+)\s*\*\s*60\s*\*\s*1000", mjs)
        check("server: served auto-lock default is 5 minutes (manual 4.1)",
              _idle is not None and int(_idle.group(1)) == 5,
              "IDLE_LOCK_MS" if _idle is None else _idle.group(0))

        # PORT override — a second demo instance bound to an ephemeral port must
        # answer on it (manual 4.1 "可用环境变量 PORT 覆盖" / 5.1 故障 6)
        _probe = _sk.socket()
        _probe.bind(("127.0.0.1", 0))
        _alt = _probe.getsockname()[1]
        _probe.close()
        _env = dict(os.environ)
        _env["PORT"] = str(_alt)
        _srv = _sp.Popen(
            ["node", "--enable-source-maps", "server/demo.mjs"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, env=_env,
        )
        try:
            _deadline = time.time() + 10
            _ok = False
            while time.time() < _deadline:
                try:
                    c2 = _hc.HTTPConnection("127.0.0.1", _alt, timeout=2)
                    c2.request("GET", "/api/health")
                    r2 = c2.getresponse()
                    h2 = json.loads(r2.read().decode("utf-8"))
                    c2.close()
                    if r2.status == 200 and h2.get("ok") is True:
                        _ok = True
                        break
                except Exception:
                    time.sleep(0.15)
            check("server: PORT env override rebinds the port (manual 4.1/故障 6)",
                  _ok, f"alt port {_alt}")
        finally:
            _srv.terminate()
            try:
                _srv.wait(timeout=5)
            finally:
                _srv.kill()

        # ---- 用户手册 2.1 "可将整个软件包根目录（含 public/ 与 src/…）部署到
        # 任意静态站点服务器（Nginx、Apache、Web 服务器等），效果一致":
        # the app must work *identically* when served by a plain static file
        # server with no demo routes at all. Everything above runs against
        # server/demo.mjs, which also answers /api/health; none of the page's
        # behavior may depend on that route. Prove it by serving the exact same
        # public/ + src/ tree from Python's stdlib http.server (bound to an
        # ephemeral port, emulating Nginx/Apache docroot resolution), then
        # driving the full create -> edit -> save -> list lifecycle over it —
        # MIME parity for the ES module graph included.
        import shutil as _sh, tempfile as _tf
        _tmpd = _tf.TemporaryDirectory()
        _repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _publ = os.path.join(_repo, "public")
        _sroot = _tmpd.name
        for _fn in ("index.html", "main.js", "idb.js", "markdown.js"):
            _sh.copyfile(os.path.join(_publ, _fn), os.path.join(_sroot, _fn))
        os.symlink(os.path.join(_repo, "src"), os.path.join(_sroot, "src"))
        _probe2 = _sk.socket()
        _probe2.bind(("127.0.0.1", 0))
        _alt2 = _probe2.getsockname()[1]
        _probe2.close()
        _srv2 = _sp.Popen(
            [sys.executable, "-m", "http.server", str(_alt2),
             "--bind", "127.0.0.1", "--directory", _sroot],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        try:
            _deadline = time.time() + 10
            _up = False
            while time.time() < _deadline:
                try:
                    _c = _hc.HTTPConnection("127.0.0.1", _alt2, timeout=2)
                    _c.request("GET", "/")
                    _r = _c.getresponse()
                    _b = _r.read().decode("utf-8", "replace")
                    _c.close()
                    if _r.status == 200 and "view-unlock" in _b:
                        _up = True
                        break
                except Exception:
                    time.sleep(0.15)
            check("static: a plain static file server serves the app (manual 2.1)",
                  _up, f"alt2 port {_alt2}")

            # the foreign server structurally cannot answer /api/health — the
            # app must need only static files, never demo.mjs's route
            _c = _hc.HTTPConnection("127.0.0.1", _alt2, timeout=5)
            _c.request("GET", "/api/health")
            _r = _c.getresponse()
            _c.close()
            check("static: app needs no /api/health route (offline by construction)",
                  _r.status == 404, f"status={_r.status}")

            # module-graph MIME parity: .js must be served as a JS MIME type or
            # Firefox would refuse the ES module and the app would never boot
            _c = _hc.HTTPConnection("127.0.0.1", _alt2, timeout=5)
            _c.request("GET", "/main.js")
            _r = _c.getresponse()
            _mjs = _r.read().decode("utf-8", "replace")
            _ct = _r.getheader("Content-Type", "")
            _c.close()
            check("static: main.js served with a JS MIME + same bytes (module graph boots)",
                  _r.status == 200 and "javascript" in _ct and "IDLE_LOCK_MS" in _mjs,
                  _ct)

            # full lifecycle on the foreign origin: create vault -> new doc ->
            # save -> the doc lists (the same AC1 path, but with no demo server
            # behind the wire). Navigating resets the page, so wait for the
            # unlock gate to re-appear, then build a vault exactly like AC1.
            driver.get(f"http://127.0.0.1:{_alt2}/")
            wait_for(driver, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            check("static: boots to the unlock gate on a foreign origin", True)
            driver.find_element(By.ID, "wsname").send_keys("static-parity")
            driver.find_element(By.ID, "pw2").send_keys("correct horse battery staple")
            driver.find_element(By.ID, "pw2b").send_keys("correct horse battery staple")
            driver.find_element(By.ID, "btn-create").click()
            wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 15)
            _lbl = driver.find_element(By.ID, "ws-label").text
            check("static: vault creates on the static origin (2.1 效果一致)",
                  "static-parity" in _lbl, _lbl)
            driver.find_element(By.ID, "btn-new").click()
            time.sleep(1.0)
            _t = driver.find_element(By.ID, "editor-title")
            _t.clear()
            _t.send_keys("Static Parity Doc")
            _a = driver.find_element(By.ID, "editor-area")
            _a.clear()
            _a.send_keys("served from a plain static file server; no demo routes.")
            driver.find_element(By.ID, "btn-save").click()
            time.sleep(1.5)
            _rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("static: create+edit+persist works end-to-end (manual 2.1)",
                  any("Static Parity Doc" in (r.text or "") for r in _rows),
                  [r.text for r in _rows])
        finally:
            _srv2.terminate()
            try:
                _srv2.wait(timeout=5)
            finally:
                _srv2.kill()

    finally:
        driver.quit()

    if failures:
        print("\nBROWSER E2E FAILURES:", failures)
        sys.exit(1)
    print("\nALL BROWSER E2E CHECKS PASSED (AC1,AC2,AC3,AC4,AC5,AC6 minimal)")


if __name__ == "__main__":
    main()
