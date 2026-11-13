"""Render docs/assets/update-flow-{light,dark}.svg. Run: python tools/make_flow_diagram.py"""

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "assets"

PALETTES = {
    "light": dict(bg="#ffffff", box="#f6f8fa", stroke="#d0d7de", text="#1f2328", muted="#59636e",
                  accent="#0969da", ok="#1a7f37", okbg="#dafbe1", warn="#bc4c00", warnbg="#fff1e5"),
    "dark": dict(bg="#0d1117", box="#161b22", stroke="#3d444d", text="#f0f6fc", muted="#9198a1",
                 accent="#4493f8", ok="#3fb950", okbg="#12261e", warn="#f0883e", warnbg="#2d1c0f"),
}

STEPS = [
    ("1  Check", ["Read the 500-byte", "release manifest"]),
    ("2  Download", ["Resumable, progress,", "optional mirror"]),
    ("3  Verify", ["Upstream validator,", "--require-publishable"]),
    ("4  Stage", ["Extract beside live", "data; no downgrade"]),
    ("5  Swap", ["Rename into place,", "real Qlib read"]),
]

W, H = 960, 330
BOX_W, BOX_H, GAP, TOP = 150, 92, 30, 70
LEFT = (W - (len(STEPS) * BOX_W + (len(STEPS) - 1) * GAP)) // 2


def svg(p: dict) -> str:
    font = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'font-family="{font}" role="img" aria-label="qlib-sync update flow">',
        "<defs>",
        f'<marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{p["muted"]}"/></marker>',
        f'<marker id="w" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{p["warn"]}"/></marker>',
        f'<marker id="g" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{p["ok"]}"/></marker>',
        "</defs>",
        f'<rect width="{W}" height="{H}" rx="12" fill="{p["bg"]}"/>',
        f'<text x="{W // 2}" y="38" text-anchor="middle" font-size="17" font-weight="600" fill="{p["text"]}">'
        "qlib-sync cn update</text>",
    ]
    xs = [LEFT + i * (BOX_W + GAP) for i in range(len(STEPS))]
    for i, (x, (title, lines)) in enumerate(zip(xs, STEPS)):
        out.append(f'<rect x="{x}" y="{TOP}" width="{BOX_W}" height="{BOX_H}" rx="10" fill="{p["box"]}" '
                   f'stroke="{p["stroke"]}"/>')
        out.append(f'<text x="{x + BOX_W / 2}" y="{TOP + 30}" text-anchor="middle" font-size="15" '
                   f'font-weight="600" fill="{p["accent"]}">{title}</text>')
        for j, line in enumerate(lines):
            out.append(f'<text x="{x + BOX_W / 2}" y="{TOP + 54 + j * 18}" text-anchor="middle" font-size="12.5" '
                       f'fill="{p["muted"]}">{line}</text>')
        if i:
            out.append(f'<line x1="{xs[i - 1] + BOX_W + 3}" y1="{TOP + BOX_H / 2}" x2="{x - 3}" '
                       f'y2="{TOP + BOX_H / 2}" stroke="{p["muted"]}" stroke-width="1.6" marker-end="url(#a)"/>')

    # Early exit from Check.
    cx, by = xs[0] + BOX_W / 2, TOP + BOX_H
    out.append(f'<line x1="{cx}" y1="{by + 3}" x2="{cx}" y2="{by + 42}" stroke="{p["ok"]}" stroke-width="1.6" '
               f'stroke-dasharray="4 3" marker-end="url(#g)"/>')
    out.append(f'<rect x="{xs[0] - 10}" y="{by + 46}" width="{BOX_W + 20}" height="52" rx="10" fill="{p["okbg"]}" '
               f'stroke="{p["ok"]}"/>')
    out.append(f'<text x="{cx}" y="{by + 67}" text-anchor="middle" font-size="13" font-weight="600" '
               f'fill="{p["ok"]}">Already current</text>')
    out.append(f'<text x="{cx}" y="{by + 86}" text-anchor="middle" font-size="12" fill="{p["muted"]}">'
               "done in ~1 s, no download</text>")

    # Failure path from Download..Swap to rollback.
    fx0, fx1 = xs[1] - 6, xs[4] + BOX_W + 6
    fy = by + 46
    for x in xs[1:]:
        mx = x + BOX_W / 2
        out.append(f'<line x1="{mx}" y1="{by + 3}" x2="{mx}" y2="{fy - 4}" stroke="{p["warn"]}" '
                   f'stroke-width="1.4" stroke-dasharray="4 3" marker-end="url(#w)"/>')
    out.append(f'<rect x="{fx0}" y="{fy}" width="{fx1 - fx0}" height="52" rx="10" fill="{p["warnbg"]}" '
               f'stroke="{p["warn"]}"/>')
    out.append(f'<text x="{(fx0 + fx1) / 2}" y="{fy + 22}" text-anchor="middle" font-size="13" font-weight="600" '
               f'fill="{p["warn"]}">Any failure: the previous dataset is put back untouched</text>')
    out.append(f'<text x="{(fx0 + fx1) / 2}" y="{fy + 41}" text-anchor="middle" font-size="12" fill="{p["muted"]}">'
               "corrupt downloads are discarded, never resumed; a lock blocks concurrent runs</text>")

    # Success summary on the right edge of the last box.
    out.append(f'<text x="{W // 2}" y="{H - 22}" text-anchor="middle" font-size="12.5" fill="{p["muted"]}">'
               f'On success: <tspan fill="{p["ok"]}" font-weight="600">new trading days, listed and delisted '
               "symbols</tspan> are reported; the old copy is removed or kept with --keep-backup</text>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, palette in PALETTES.items():
        (OUT / f"update-flow-{name}.svg").write_text(svg(palette))
        print(OUT / f"update-flow-{name}.svg")
