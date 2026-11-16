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

        # ---- P1 editor: markdown preview toggle + XSS safety (real browser)
        # The current doc is open in the editor at this point.
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys(
            '\n## MD Preview Check\n**bold** `code` [bad](javascript:alert(1))\n<script>alert(1)</script>'
        )
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.6)
        prev = driver.find_element(By.ID, "preview")
        inner = prev.get_attribute("innerHTML")
        check("preview renders markdown headings", "<h2>MD Preview Check</h2>" in inner, inner[:200])
        check("preview renders inline formatting", "<strong>bold</strong>" in inner)
        check("preview escapes raw HTML inert", "<script>alert" not in inner and "&lt;script&gt;" in inner)
        check("preview rejects javascript: links", "<em>bad</em>" in inner and "javascript:" not in inner)
        check("preview toggle button flips to Edit", driver.find_element(By.ID, "btn-preview").text == "Edit")
        driver.find_element(By.ID, "btn-preview").click()  # back to editor
        time.sleep(0.4)
        check("preview toggle returns to editor",
              driver.find_element(By.ID, "editor-area").is_displayed())

        # ---- table blocks: pipe rows round-trip editor -> encrypted store
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys("\n\n| city | country |\n| --- | --- |\n| Kyoto | Japan |")
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.2)
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.6)
        prev = driver.find_element(By.ID, "preview")
        inner = prev.get_attribute("innerHTML")
        check("table block previews as <table>",
              "<table>" in inner and "<th>city</th>" in inner and "<td>Kyoto</td>" in inner,
              inner[:200])
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

        # ---- AC2: IndexedDB holds no plaintext
        dump = flatten_dump(get_db_dump(driver))
        for needle in [KNOWN_TITLE, "oscar-7", "48.8584N", "deflector", "Project Snowstorm", "Kyoto"]:
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
        driver.find_element(By.ID, "btn-search").click()
        time.sleep(1.5)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("AC5: search hits the doc by keyword",
              any(KNOWN_TITLE in (r.text or "") for r in rows), [r.text for r in rows])

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

        # IndexedDB must hold ciphertext only, with no new-password plaintext
        dump = flatten_dump(get_db_dump(driver))
        for needle in [NEW_PW, "correct horse battery staple"]:
            check(f"re-key: no master password plaintext in IndexedDB ('{needle}')", needle not in dump)

        # ---- AC6: offline — assert page JS/import loads (no network fallback)
        # The page itself is a static module graph served from /public; language
        # server already covered by reloads above. Also verify demo serves offline
        # static files with no external CDN references.
        html = driver.page_source
        check("AC6: page references no external network",
              all(x not in html for x in ["https://", "//cdn", "http://"]))

    finally:
        driver.quit()

    if failures:
        print("\nBROWSER E2E FAILURES:", failures)
        sys.exit(1)
    print("\nALL BROWSER E2E CHECKS PASSED (AC1,AC2,AC3,AC4,AC5,AC6 minimal)")


if __name__ == "__main__":
    main()
