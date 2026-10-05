"""The enrollment page a capture-access link opens.

Static: the capture URL and token arrive in the URL fragment, which the
browser never sends here, and the page hands them to the client app through
its own deep link. Nothing about the device or the token reaches the server.
"""

ENROLL_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Connect Cochlea</title>
<style>
  :root { color-scheme: light dark; --fg: #111; --bg: #fafafa; --muted: #666; --accent: #6d28d9; }
  @media (prefers-color-scheme: dark) { :root { --fg: #eee; --bg: #111; --muted: #999; --accent: #a78bfa; } }
  body { margin: 0; min-height: 100dvh; display: grid; place-items: center; background: var(--bg);
         color: var(--fg); font: 16px/1.5 -apple-system, BlinkMacSystemFont, system-ui, sans-serif; }
  main { max-width: 26rem; padding: 2rem 1.5rem; text-align: center; }
  h1 { font-size: 1.5rem; margin: 0 0 .5rem; }
  p { color: var(--muted); margin: 0 0 1.5rem; }
  a.button { display: inline-block; padding: .8rem 1.6rem; border-radius: .75rem; background: var(--accent);
             color: #fff; text-decoration: none; font-weight: 600; }
  #install { margin: 1.5rem 0 0; font-size: .9rem; }
  code { font-size: .85rem; }
</style>
</head>
<body>
<main>
  <h1>Connect Cochlea</h1>
  <p id="hint">Open this link on the device you want to connect, with Cochlea installed.</p>
  <a class="button" id="open" href="#" hidden>Open in Cochlea</a>
  <p id="install" hidden>Not installed? Ask the person who sent this link for an install link.</p>
</main>
<script>
  const params = new URLSearchParams(location.hash.slice(1));
  const url = params.get("url"), token = params.get("token");
  if (url && token) {
    history.replaceState(null, "", location.pathname);  // keep the token out of the address bar
    const link = "offlineshazam://enroll?" + new URLSearchParams({ url, token });
    const open = document.getElementById("open");
    open.href = link; open.hidden = false;
    document.getElementById("install").hidden = false;
    document.getElementById("hint").textContent = "This connects Cochlea on this device to Music Sync.";
  } else {
    document.getElementById("hint").textContent = "This link is incomplete. Ask for a new one.";
  }
</script>
</body>
</html>
"""
