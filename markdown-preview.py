# ~/.local/share/xed/plugins/markdown_preview.py
from gi import require_version
from gi.repository import GObject, Gtk, GLib
from gi.repository import Xed

# Try 4.1 first (Mint 22/Ubuntu 24.04+), fall back to 4.0 if needed
try:
    require_version('WebKit2', '4.1')
except Exception:
    require_version('WebKit2', '4.0')
from gi.repository import WebKit2

# Try to use 'markdown' if present; fall back to a super-light converter
try:
    import markdown  # python3-markdown
    def md_to_html(text: str) -> str:
        return markdown.markdown(
            text,
            extensions=["extra", "codehilite", "tables", "toc"]
        )
except Exception:
    # Minimal fallback: escape + very naive link/code handling
    import html, re
    def md_to_html(text: str) -> str:
        t = html.escape(text)
        # very basic formatting so preview isn't totally plain
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", t)
        t = re.sub(r"^# (.+)$", r"<h1>\1</h1>", t, flags=re.MULTILINE)
        t = re.sub(r"^## (.+)$", r"<h2>\1</h2>", t, flags=re.MULTILINE)
        t = re.sub(r"^### (.+)$", r"<h3>\1</h3>", t, flags=re.MULTILINE)
        t = re.sub(r"(https?://\\S+)", r'<a href="\\1">\\1</a>', t)
        t = t.replace("\n", "<br/>")
        return t

LIGHT_CSS = """
:root { color-scheme: light; }
body { font-family: system-ui, sans-serif; margin: 1.25rem; line-height: 1.45; }
pre, code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
pre { padding: .75rem; overflow:auto; border: 1px solid #e5e5e5; border-radius: 6px; background:#fafafa; }
code { background:#f4f4f4; padding: .1rem .25rem; border-radius: 4px; }
h1,h2,h3 { margin-top:1.2em; }
table { border-collapse: collapse; }
td, th { border: 1px solid #ddd; padding: .4rem .6rem; }
a { text-decoration: none; }
a:hover { text-decoration: underline; }
"""

DARK_CSS = """
:root { color-scheme: dark; }
body { font-family: system-ui, sans-serif; margin: 1.25rem; line-height: 1.45; color: #e6e6e6; background:#121212; }
pre, code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
pre { padding: .75rem; overflow:auto; border: 1px solid #333; border-radius: 6px; background:#1e1e1e; }
code { background:#222; padding: .1rem .25rem; border-radius: 4px; }
h1,h2,h3 { margin-top:1.2em; color:#fff; }
table { border-collapse: collapse; }
td, th { border: 1px solid #333; padding: .4rem .6rem; }
a { color:#9bcaff; text-decoration: none; }
a:hover { text-decoration: underline; }
"""

HTML_SHELL = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>{css}</style>
</head>
<body>
{body}
</body>
</html>
"""

DEBOUNCE_MS = 250


class MarkdownPreview(GObject.Object, Xed.WindowActivatable):
    __gtype_name__ = "MarkdownPreview"
    window = GObject.Property(type=Xed.Window)

    def __init__(self):
        super().__init__()
        self._panel_item = None
        self._sw = None
        self._web = None
        self._timeout_id = 0
        self._buffer_handler_ids = []
        self._window_handlers = []
        self._connected_doc = None

    def _is_markdown_doc(self, doc):
        #print("markdown-preview: Checking if document is a markdown")
        if not doc:
            #print("markdown-preview: No document available, not activating markdown plugin")
            return False
        try:
            gfile = doc.get_location()
            if gfile:
                name = gfile.get_basename().lower()
                if name.endswith((".md", ".markdown")):
                    return True
            #else:
            #    print("markdown-preview: Doc location unknown")
            # MIME-type fallback (works for unsaved buffers)
            content_type = doc.get_content_type()
            if content_type and "markdown" in content_type.lower():
                return True
            #else:
            #    print("markdown-preview: content_type = {}".format(content_type))
        except Exception as ex:
            #pass
            print("markdown-preview: {}".format(ex))
        return False
    
    # ---- lifecycle ----
    def do_activate(self):
        # Create preview widgetry
        self._web = WebKit2.WebView()
        self._sw = Gtk.ScrolledWindow()
        self._sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._sw.add(self._web)
        self._sw.show_all()

        # Add as a bottom panel tab
        bottom = self.window.get_bottom_panel()
        self._panel_item = bottom.add_item(
            self._sw,
            "xed-markdown-preview",
            "Markdown Preview",
            # "text-x-generic"  # a generic icon name that should exist
        )
        bottom.set_visible(True)
        bottom.activate_item(self._sw)

        # Listen to window/tab changes
        self._window_handlers.append(self.window.connect("active-tab-changed", self._on_active_tab_changed))
        self._window_handlers.append(self.window.connect("tab-added", self._on_tab_changed))
        self._window_handlers.append(self.window.connect("tab-removed", self._on_tab_changed))

        # Hook current document
        self._reconnect_to_active_buffer()
        self._render_now()

    def do_deactivate(self):
        self._disconnect_from_buffer()
        for hid in self._window_handlers:
            self.window.disconnect(hid)
        self._window_handlers.clear()

        if self._panel_item is not None:
            bottom = self.window.get_bottom_panel()
            bottom.remove_item(self._sw)
            self._panel_item = None

        self._web = None
        self._sw = None

        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = 0

    # ---- helpers ----
    def _on_active_tab_changed(self, window, tab):
        self._reconnect_to_active_buffer()
        self._render_now()

    def _on_tab_changed(self, *args):
        self._reconnect_to_active_buffer()
        self._render_now()

    def _reconnect_to_active_buffer(self):
        self._disconnect_from_buffer()

        doc = self.window.get_active_document()
        if doc is None:
            return

        # (Optional) only hook when it's Markdown
        if not self._is_markdown_doc(doc):
            return

        # Connect signals ON THIS doc and remember it
        self._connected_doc = doc
        self._buffer_handler_ids = [
            doc.connect("changed", self._on_buffer_changed),
            doc.connect("mark-set", self._on_buffer_changed),
        ]

    def _disconnect_from_buffer(self):
        if not self._connected_doc:
            self._buffer_handler_ids.clear()
            return

        # Only disconnect from the SAME doc we connected earlier
        for hid in self._buffer_handler_ids:
            try:
                if GObject.signal_handler_is_connected(self._connected_doc, hid):
                    self._connected_doc.disconnect(hid)
            except Exception:
                pass

        self._buffer_handler_ids.clear()
        self._connected_doc = None

    def _on_buffer_changed(self, *args):
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
        self._timeout_id = GLib.timeout_add(DEBOUNCE_MS, self._render_now)

    def _get_active_text_and_baseuri(self):
        doc = self.window.get_active_document()
        #if not doc:
        #    return "", None
        if not self._is_markdown_doc(doc):
            return  "", None
        start = doc.get_start_iter()
        end = doc.get_end_iter()
        text = doc.get_text(start, end, True)
        # base URI so relative links/images resolve
        location = None
        try:
            # XedDocuments usually support get_location() like Gedit
            gfile = doc.get_location()
            if gfile:
                uri = gfile.get_uri()
                # For base URI we want the directory:
                # Use parent if available; WebKit accepts file:// URIs
                parent = gfile.get_parent()
                if parent:
                    location = parent.get_uri()
        except Exception:
            pass
        return text, location

    def _choose_css(self):
        # follow dark preference if app is dark
        settings = Gtk.Settings.get_default()
        prefer_dark = False
        if settings:
            try:
                prefer_dark = settings.props.gtk_application_prefer_dark_theme
            except Exception:
                pass
        return DARK_CSS if prefer_dark else LIGHT_CSS

    def _render_now(self):
        self._timeout_id = 0
        if not self._web:
            return False

        text, base = self._get_active_text_and_baseuri()
        # Render markdown to HTML
        body = md_to_html(text)
        html = HTML_SHELL.format(css=self._choose_css(), body=body)
        try:
            self._web.load_html(html, base if base else "about:blank")
        except Exception:
            # best effort; avoid crashing the plugin
            pass
        return False
