"""Every red x that deletes something asks first.

A red x on a grid row removes that row -- a rule, a team, an account, a key.
One stray click must not do that silently, so each x's click handler shows a
confirmation (askConfirm) before it changes anything. This reads app.js: it
finds every button drawn as a red x, then that button's handler, and checks
the handler confirms.

Exempt, and listed here so a new one is a decision rather than an accident:
the x's that clear a search or a filter, and the lines of a split being built
in its dialog (nothing is saved until the dialog's Save, and Cancel discards
them).
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "app/static/app.js"), encoding="utf-8").read()
LINES = JS.split("\n")

EXEMPT = {
    # clear a search box or a filter
    "gi-clear", "fchip-x", "fchip-mx", "cardFindClear", "ctFindX", "pfFindX",
    # cancel an edit in place
    "ed-cancel",
    # a line of a split still being built in its dialog
    "bsDel", "spDel", "rsDel",
}
RED = re.compile(r"var\(--bad\)|#dc2626|#b91c1c")
X_BTN = re.compile(r"<button\b([^>]*)>\s*(?:&times;|\\u00d7|×|✕|\\u2715)\s*</button>", re.S)

ok = True


def check(label, cond, got=""):
    global ok
    ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond else f"  -> {got}"))


classes = set()
for m in X_BTN.finditer(JS):
    attrs = m.group(1)
    if not RED.search(attrs):
        continue
    cls = re.search(r'class="([^"]*)"', attrs)
    if not cls:
        continue
    names = [c for c in cls.group(1).split() if c not in ("ghost",)]
    if any(c in EXEMPT for c in names):
        continue
    classes.update(c for c in names if not c.startswith("gi-"))

check("red x buttons found", len(classes) >= 10, sorted(classes))


def handler_blocks(cls):
    """The click handlers bound to this class: a few lines from each binding."""
    pat = re.compile(r"""['"`.]%s['"`\]\s]""" % re.escape(cls))
    out = []
    for i, line in enumerate(LINES):
        if pat.search(line) and re.search(r"onclick|addEventListener\('click'|closest\(", line) \
                and "<button" not in line:
            out.append("\n".join(LINES[i:i + 26]))
    return out


for cls in sorted(classes):
    blocks = handler_blocks(cls)
    check(f"{cls}: has a click handler", bool(blocks))
    if blocks:
        check(f"{cls}: confirms before deleting", all("askConfirm" in b for b in blocks),
              [b.splitlines()[0].strip()[:90] for b in blocks if "askConfirm" not in b])

# ONE LOOK FOR REMOVING A ROW: a red x, never a "Remove" or "Delete" word
# button beside other grids' x's.
words = [JS.count('\n', 0, m.start()) + 1 for m in re.finditer(
    r"<button\b[^>]*>\s*(?:Remove|Delete)\s*</button>", JS, re.S)]
check("no grid row uses a Remove / Delete word button (all red x)", not words, words)

print("\nALL CHECKS PASSED" if ok else "\n*** FAILURES ***")
sys.exit(0 if ok else 1)
