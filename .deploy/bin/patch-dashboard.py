#!/usr/bin/env python3
"""
Patch the dashboard frontend at startup for Radeon Cloud proxy compatibility.

Instead of shipping a static index.html with hardcoded asset hashes (which
break every time the dashboard image is rebuilt), this script:

1. Reads the *real* index.html from the dashboard image (with correct hashes)
2. Makes all asset references relative (./) so they work behind proxy
3. Injects the proxy shim (URL rewriting, auth injection, auto-login)
4. Patches the JS bundle for BrowserRouter basename + Vite chunk resolver
5. Eagerly preloads route CSS by scanning the assets directory

This runs once at startup (idempotent — skips if already patched).
"""

import glob
import os
import re
import sys

FRONTEND_DIR = sys.argv[1] if len(sys.argv) > 1 else "/opt/vllm-sr/frontend"
ASSETS_DIR = os.path.join(FRONTEND_DIR, "assets")
INDEX_HTML = os.path.join(FRONTEND_DIR, "index.html")
MARKER = "__vsr_subpath_shim"


def already_patched():
    """Check if the shim has already been injected."""
    if not os.path.exists(INDEX_HTML):
        print(f"  ERROR: {INDEX_HTML} not found", file=sys.stderr)
        sys.exit(1)
    with open(INDEX_HTML) as f:
        return MARKER in f.read()


def make_refs_relative(html):
    """Convert absolute asset refs to relative so they resolve via proxy."""
    html = html.replace('href="/', 'href="./')
    html = html.replace("href='/", "href='./")
    html = html.replace('src="/', 'src="./')
    html = html.replace("src='/", "src='./")
    return html


def discover_route_css():
    """
    Scan assets/ for route-specific CSS files (DashboardPage-*.css, etc.)
    and return <link> tags to eagerly preload them.

    This replaces hardcoded CSS filenames — whatever the dashboard ships,
    we discover and preload.
    """
    route_patterns = [
        "DashboardPage-*.css",
        "PlaygroundPage-*.css",
        "BuilderPage-*.css",
        "StatusPage-*.css",
        "SettingsPage-*.css",
        "LoginPage-*.css",
    ]
    tags = []
    for pattern in route_patterns:
        for path in sorted(glob.glob(os.path.join(ASSETS_DIR, pattern))):
            filename = os.path.basename(path)
            tags.append(
                f'    <link rel="stylesheet" crossorigin href="./assets/{filename}">'
            )
    return "\n".join(tags)


# ── The proxy shim (injected into <head>) ──────────────────────────────────
# This is the same shim from the old static index.html, kept as a Python
# string so we can inject it into any dashboard version.
PROXY_SHIM = f'''    <script data-id="{MARKER}">
    (function() {{
      function getProxyBase() {{
        var p = window.location.pathname;
        var m = p.match(/\\/proxy\\/\\d+/);
        if (m) return p.substring(0, m.index + m[0].length + 1);
        return '';
      }}
      var PROXY_BASE = getProxyBase();

      var ORIGIN = window.location.origin;
      function rewriteUrl(url) {{
        if (typeof url !== 'string' || !PROXY_BASE) return url;
        if (url.startsWith('/') && !url.startsWith(PROXY_BASE)) {{
          return PROXY_BASE + url.substring(1);
        }}
        if (url.startsWith(ORIGIN + '/') && url.indexOf(PROXY_BASE) === -1) {{
          return ORIGIN + PROXY_BASE + url.substring(ORIGIN.length + 1);
        }}
        return url;
      }}

      // Intercept fetch — rewrite URLs, inject auth, and fake /api/auth/me if needed
      var origFetch = window.fetch;
      window.fetch = function(url, opts) {{
        if (typeof url === 'string') url = rewriteUrl(url);
        else if (url instanceof Request) {{
          var u = new URL(url.url);
          var rw = rewriteUrl(u.pathname);
          if (rw !== u.pathname) {{ u.pathname = rw; url = new Request(u.toString(), url); }}
        }}
        var resolvedUrl = (typeof url === 'string') ? url : (url instanceof Request ? url.url : '');

        if (PROXY_BASE && resolvedUrl.indexOf('/api/') !== -1) {{
          var t = localStorage.getItem('token');
          if (t) {{
            opts = opts || {{}};
            opts.headers = new Headers(opts.headers || {{}});
            if (!opts.headers.has('Authorization')) {{
              opts.headers.set('Authorization', 'Bearer ' + t);
            }}
          }}
        }}

        if (PROXY_BASE && resolvedUrl.indexOf('/api/auth/me') !== -1) {{
          var storedUser = localStorage.getItem('user');
          var storedToken = localStorage.getItem('token');
          if (storedUser && storedToken) {{
            return origFetch.call(this, url, opts).then(function(resp) {{
              if (resp.ok) return resp;
              console.log('[proxy-auth] /api/auth/me returned', resp.status, '— injecting cached user');
              var userData = JSON.parse(storedUser);
              var body = JSON.stringify({{ user: userData, token: storedToken }});
              return new Response(body, {{
                status: 200,
                statusText: 'OK',
                headers: {{ 'Content-Type': 'application/json' }}
              }});
            }});
          }}
        }}

        return origFetch.call(this, url, opts);
      }};

      // Intercept XMLHttpRequest
      var origXHR = XMLHttpRequest.prototype.open;
      var origXHRSend = XMLHttpRequest.prototype.send;
      var origSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;
      XMLHttpRequest.prototype.open = function(method, url) {{
        this._vsrUrl = url;
        this._vsrHasAuth = false;
        var args = Array.prototype.slice.call(arguments);
        args[1] = rewriteUrl(url);
        return origXHR.apply(this, args);
      }};
      XMLHttpRequest.prototype.setRequestHeader = function(name, value) {{
        if (name.toLowerCase() === 'authorization') this._vsrHasAuth = true;
        return origSetRequestHeader.apply(this, arguments);
      }};
      XMLHttpRequest.prototype.send = function() {{
        var self = this;
        if (PROXY_BASE && !this._vsrHasAuth && this._vsrUrl && this._vsrUrl.indexOf('/api/') !== -1) {{
          var t = localStorage.getItem('token');
          if (t) origSetRequestHeader.call(this, 'Authorization', 'Bearer ' + t);
        }}
        if (PROXY_BASE && this._vsrUrl && this._vsrUrl.indexOf('/api/auth/me') !== -1) {{
          var storedUser = localStorage.getItem('user');
          var storedToken = localStorage.getItem('token');
          if (storedUser && storedToken) {{
            this.addEventListener('load', function() {{
              if (self.status === 401) {{
                console.log('[proxy-auth] XHR /api/auth/me returned 401 — overriding');
                var body = JSON.stringify({{ user: JSON.parse(storedUser), token: storedToken }});
                Object.defineProperty(self, 'status', {{ value: 200 }});
                Object.defineProperty(self, 'statusText', {{ value: 'OK' }});
                Object.defineProperty(self, 'responseText', {{ value: body }});
                Object.defineProperty(self, 'response', {{ value: body }});
              }}
            }});
          }}
        }}
        return origXHRSend.apply(this, arguments);
      }};

      // Set <base> tag for proxy path
      if (PROXY_BASE) {{
        var baseEl = document.createElement('base');
        baseEl.href = PROXY_BASE;
        document.head.prepend(baseEl);
        console.log('[proxy] Set <base href="' + PROXY_BASE + '">');

        window.__VSR_BASE = PROXY_BASE.replace(/\\/$/, '');
        console.log('[proxy] Set __VSR_BASE="' + window.__VSR_BASE + '"');
      }}

      // Intercept dynamic element src/href via setAttribute
      var origSetAttr = Element.prototype.setAttribute;
      Element.prototype.setAttribute = function(name, value) {{
        if ((name === 'src' || name === 'href') && typeof value === 'string') {{
          value = rewriteUrl(value);
        }}
        return origSetAttr.call(this, name, value);
      }};

      // Intercept .src property setter
      ['HTMLScriptElement', 'HTMLImageElement', 'HTMLSourceElement', 'HTMLMediaElement', 'HTMLIFrameElement'].forEach(function(cls) {{
        var proto = window[cls] && window[cls].prototype;
        if (!proto) return;
        var desc = Object.getOwnPropertyDescriptor(proto, 'src');
        if (desc && desc.set) {{
          var origSet = desc.set;
          Object.defineProperty(proto, 'src', {{
            set: function(v) {{ origSet.call(this, rewriteUrl(v)); }},
            get: desc.get,
            configurable: true,
            enumerable: true
          }});
        }}
      }});

      // Intercept .href property setter on link elements
      var linkDesc = Object.getOwnPropertyDescriptor(HTMLLinkElement.prototype, 'href');
      if (linkDesc && linkDesc.set) {{
        var origLinkSet = linkDesc.set;
        Object.defineProperty(HTMLLinkElement.prototype, 'href', {{
          set: function(v) {{ origLinkSet.call(this, rewriteUrl(v)); }},
          get: linkDesc.get,
          configurable: true,
          enumerable: true
        }});
      }}

      function needsRewrite(val) {{
        if (!val || !PROXY_BASE) return false;
        if (val.startsWith('/') && !val.startsWith(PROXY_BASE)) return true;
        if (val.startsWith(ORIGIN + '/') && val.indexOf(PROXY_BASE) === -1) return true;
        return false;
      }}

      // MutationObserver fallback
      if (PROXY_BASE) {{
        new MutationObserver(function(mutations) {{
          mutations.forEach(function(m) {{
            m.addedNodes.forEach(function(node) {{
              if (node.nodeType !== 1) return;
              var els = [node];
              if (node.querySelectorAll) els = els.concat(Array.from(node.querySelectorAll('[src],[href]')));
              els.forEach(function(el) {{
                ['src', 'href'].forEach(function(attr) {{
                  var val = el.getAttribute(attr);
                  if (needsRewrite(val)) {{
                    origSetAttr.call(el, attr, rewriteUrl(val));
                  }}
                }});
              }});
            }});
          }});
        }}).observe(document.documentElement, {{ childList: true, subtree: true }});
      }}

      // Intercept URL constructor
      var OrigURL = window.URL;
      if (PROXY_BASE) {{
        window.URL = function(url, base) {{
          var result = (arguments.length > 1) ? new OrigURL(url, base) : new OrigURL(url);
          if (result.origin === ORIGIN && result.pathname.startsWith('/assets/') && result.pathname.indexOf(PROXY_BASE) === -1) {{
            return new OrigURL(ORIGIN + PROXY_BASE + result.pathname.substring(1) + result.search + result.hash);
          }}
          return result;
        }};
        window.URL.prototype = OrigURL.prototype;
        window.URL.createObjectURL = OrigURL.createObjectURL;
        window.URL.revokeObjectURL = OrigURL.revokeObjectURL;
        Object.keys(OrigURL).forEach(function(k) {{
          if (!(k in window.URL)) window.URL[k] = OrigURL[k];
        }});
      }}

      // Intercept WebSocket
      var OrigWS = window.WebSocket;
      window.WebSocket = function(url, protocols) {{
        if (typeof url === 'string') {{
          var wsUrl = new OrigURL(url, window.location.href);
          wsUrl.pathname = rewriteUrl(wsUrl.pathname);
          url = wsUrl.toString();
        }}
        return protocols ? new OrigWS(url, protocols) : new OrigWS(url);
      }};
      window.WebSocket.prototype = OrigWS.prototype;

      // Auto-login
      (function autoLogin() {{
        if (!PROXY_BASE) return;
        var onLoginPage = window.location.pathname.indexOf('/login') !== -1
                       || window.location.pathname.indexOf('/signin') !== -1;

        var redirectKey = '_vsr_autologin_redirected';
        var alreadyRedirected = sessionStorage.getItem(redirectKey);

        var loginUrl = PROXY_BASE + 'api/auth/login';
        var xhr = new XMLHttpRequest();
        origXHR.call(xhr, 'POST', loginUrl, false);
        xhr.setRequestHeader('Content-Type', 'application/json');
        var gotToken = false;
        try {{
          xhr.send(JSON.stringify({{email: 'admin@workshop.local', password: 'workshop2026'}}));
          console.log('[auto-login] POST', loginUrl, '→', xhr.status);
          if (xhr.status === 200) {{
            var resp = JSON.parse(xhr.responseText);
            console.log('[auto-login] Response keys:', Object.keys(resp));
            var token = null;
            for (var key in resp) {{
              if (resp.hasOwnProperty(key) && typeof resp[key] === 'string' && resp[key].length > 20) {{
                token = resp[key];
                localStorage.setItem(key, token);
                console.log('[auto-login] Stored localStorage["' + key + '"] (' + token.length + ' chars)');
              }}
              if (typeof resp[key] === 'object' && resp[key] !== null) {{
                localStorage.setItem(key, JSON.stringify(resp[key]));
                console.log('[auto-login] Stored localStorage["' + key + '"] (object)');
              }}
            }}
            if (token) {{
              ['auth_token', 'token', 'access_token', 'jwt_token'].forEach(function(k) {{
                localStorage.setItem(k, token);
              }});
              document.cookie = 'auth_token=' + token + '; path=/';
              document.cookie = 'token=' + token + '; path=/';
              gotToken = true;
            }}
          }}
        }} catch(e) {{
          console.warn('[auto-login] Failed:', e);
        }}

        if (onLoginPage && gotToken && !alreadyRedirected) {{
          sessionStorage.setItem(redirectKey, '1');
          console.log('[auto-login] Redirecting to dashboard root');
          window.location.replace(PROXY_BASE);
          return;
        }}

        if (onLoginPage && alreadyRedirected) {{
          console.warn('[auto-login] Already redirected once, not looping.');
          console.warn('[auto-login] localStorage keys:', Object.keys(localStorage));
        }}
      }})();
    }})();
    </script>'''


def patch_js_bundles():
    """Patch the dashboard JS bundles for proxy compatibility."""
    # Patch 1: BrowserRouter basename
    for main_js in glob.glob(os.path.join(ASSETS_DIR, "index-*.js")):
        js = open(main_js).read()
        old = "(0,N.jsx)(h,{children:"
        new = '(0,N.jsx)(h,{basename:window.__VSR_BASE||"/",children:'
        if old in js:
            js = js.replace(old, new, 1)
            open(main_js, "w").write(js)
            print(f"  patch: BrowserRouter basename -> {os.path.basename(main_js)}")

    # Patch 2: Vite chunk resolver p()
    bt = chr(96)  # backtick
    for vendor_js in glob.glob(os.path.join(ASSETS_DIR, "react-vendor-*.js")):
        js = open(vendor_js).read()
        old = f"p=function(e){{return{bt}/{bt}+e}}"
        new = f"p=function(e){{return(window.__VSR_BASE||{bt}{bt})+{bt}/{bt}+e}}"
        if old in js:
            js = js.replace(old, new, 1)
            open(vendor_js, "w").write(js)
            print(f"  patch: Vite chunk resolver -> {os.path.basename(vendor_js)}")


def patch_index_html():
    """Inject proxy shim and route CSS preloads into index.html."""
    with open(INDEX_HTML) as f:
        html = f.read()

    # Make asset references relative
    html = make_refs_relative(html)

    # Discover and build route CSS preload tags
    route_css = discover_route_css()
    css_block = ""
    if route_css:
        css_block = (
            "\n    <!-- Eagerly preload route CSS (auto-discovered) -->\n"
            + route_css
            + "\n"
        )

    # Inject the proxy shim right after <head>
    # (before any other scripts so it runs first)
    html = html.replace("<head>", "<head>\n" + PROXY_SHIM, 1)

    # Inject route CSS preloads before </head>
    if css_block:
        html = html.replace("</head>", css_block + "  </head>", 1)

    with open(INDEX_HTML, "w") as f:
        f.write(html)

    print(f"  patch: proxy shim + {len(route_css.splitlines()) if route_css else 0} route CSS preloads -> index.html")


def main():
    if already_patched():
        print("Dashboard already patched (shim marker found), skipping.")
        return

    print("Patching dashboard frontend for Radeon Cloud proxy...")
    patch_js_bundles()
    patch_index_html()
    print("Dashboard patching complete.")


if __name__ == "__main__":
    main()
