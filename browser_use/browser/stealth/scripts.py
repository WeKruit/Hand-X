"""Anti-detection JavaScript payloads injected via Page.addScriptToEvaluateOnNewDocument.

Each constant is a self-contained JS snippet that patches a single browser API surface
commonly fingerprinted by bot-detection systems.  The ``get_stealth_scripts`` helper
returns only the scripts enabled by the provided ``StealthConfig``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from browser_use.browser.stealth.config import StealthConfig

# ---------------------------------------------------------------------------
# 1. navigator.webdriver
# ---------------------------------------------------------------------------

WEBDRIVER_PATCH = """\
(() => {
  // Hide navigator.webdriver (Chromium sets this to true for automated sessions)
  Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
    configurable: true,
  });
  // Also remove it from the Navigator prototype to defeat hasOwnProperty checks
  try {
    const proto = Object.getPrototypeOf(navigator);
    if (proto && 'webdriver' in proto) {
      delete proto.webdriver;
    }
  } catch (_) {}
})();
"""

# ---------------------------------------------------------------------------
# 2. window.chrome.runtime
# ---------------------------------------------------------------------------

CHROME_RUNTIME_PATCH = """\
(() => {
  // Ensure window.chrome exists
  if (!window.chrome) {
    Object.defineProperty(window, 'chrome', {
      value: {},
      writable: true,
      configurable: true,
    });
  }
  // Stub out chrome.runtime so fingerprinters see a "real" Chrome extension API
  if (!window.chrome.runtime) {
    window.chrome.runtime = {
      connect: function () { return { onMessage: { addListener: function () {} }, postMessage: function () {} }; },
      sendMessage: function (_msg, _opts, cb) { if (typeof cb === 'function') cb(); },
      getManifest: function () { return {}; },
      getURL: function (path) { return 'chrome-extension://internal/' + path; },
      id: undefined,
      onMessage: { addListener: function () {}, removeListener: function () {} },
      onConnect: { addListener: function () {}, removeListener: function () {} },
    };
  }
  // Also ensure chrome.csi and chrome.loadTimes exist (older fingerprint checks)
  if (!window.chrome.csi) {
    window.chrome.csi = function () { return {}; };
  }
  if (!window.chrome.loadTimes) {
    window.chrome.loadTimes = function () { return {}; };
  }
})();
"""

# ---------------------------------------------------------------------------
# 3. navigator.plugins
# ---------------------------------------------------------------------------

PLUGINS_PATCH = """\
(() => {
  // Spoof navigator.plugins with entries a real Chrome install would have
  const fakePlugins = [
    { name: 'Chrome PDF Viewer', description: 'Portable Document Format', filename: 'internal-pdf-viewer',
      mimeTypes: [{ type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }] },
    { name: 'Chromium PDF Viewer', description: 'Portable Document Format', filename: 'internal-pdf-viewer',
      mimeTypes: [{ type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: '' }] },
    { name: 'Native Client', description: '', filename: 'internal-nacl-plugin',
      mimeTypes: [
        { type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable' },
        { type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable' },
      ] },
  ];

  function makeMimeType(mt) {
    const obj = Object.create(MimeType.prototype);
    Object.defineProperties(obj, {
      type:        { get: () => mt.type },
      suffixes:    { get: () => mt.suffixes },
      description: { get: () => mt.description },
      enabledPlugin: { get: () => null },
    });
    return obj;
  }

  function makePlugin(p) {
    const mimes = p.mimeTypes.map(makeMimeType);
    const obj = Object.create(Plugin.prototype);
    Object.defineProperties(obj, {
      name:        { get: () => p.name },
      description: { get: () => p.description },
      filename:    { get: () => p.filename },
      length:      { get: () => mimes.length },
    });
    mimes.forEach((m, i) => {
      Object.defineProperty(obj, i, { get: () => m, enumerable: true });
    });
    return obj;
  }

  const plugins = fakePlugins.map(makePlugin);
  const pluginArray = Object.create(PluginArray.prototype);
  Object.defineProperties(pluginArray, {
    length: { get: () => plugins.length },
    item:   { value: (i) => plugins[i] || null },
    namedItem: { value: (name) => plugins.find(p => p.name === name) || null },
    refresh: { value: () => {} },
  });
  plugins.forEach((p, i) => {
    Object.defineProperty(pluginArray, i, { get: () => p, enumerable: true });
  });

  Object.defineProperty(navigator, 'plugins', { get: () => pluginArray, configurable: true });
})();
"""

# ---------------------------------------------------------------------------
# 4. navigator.languages
# ---------------------------------------------------------------------------

LANGUAGES_PATCH = """\
(() => {
  Object.defineProperty(navigator, 'languages', {
    get: () => Object.freeze(['en-US', 'en']),
    configurable: true,
  });
  Object.defineProperty(navigator, 'language', {
    get: () => 'en-US',
    configurable: true,
  });
})();
"""

# ---------------------------------------------------------------------------
# 5. Notification.permission & navigator.permissions.query
# ---------------------------------------------------------------------------

PERMISSIONS_PATCH = """\
(() => {
  // Notification.permission should return 'default' (not 'denied' which headless uses)
  try {
    Object.defineProperty(Notification, 'permission', {
      get: () => 'default',
      configurable: true,
    });
  } catch (_) {}

  // Patch navigator.permissions.query to return realistic results
  const originalQuery = navigator.permissions && navigator.permissions.query
    ? navigator.permissions.query.bind(navigator.permissions)
    : null;

  if (navigator.permissions) {
    navigator.permissions.query = function (params) {
      if (params && params.name === 'notifications') {
        return Promise.resolve({ state: 'prompt', onchange: null });
      }
      if (originalQuery) {
        return originalQuery(params);
      }
      return Promise.resolve({ state: 'prompt', onchange: null });
    };
  }
})();
"""

# ---------------------------------------------------------------------------
# 6. WebGL renderer / vendor strings
# ---------------------------------------------------------------------------

WEBGL_PATCH = """\
(() => {
  const SPOOFED_RENDERER = 'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.1)';
  const SPOOFED_VENDOR = 'Google Inc. (Intel)';

  const getParameterProto = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (param) {
    const ext = this.getExtension('WEBGL_debug_renderer_info');
    if (ext) {
      if (param === ext.UNMASKED_RENDERER_WEBGL) return SPOOFED_RENDERER;
      if (param === ext.UNMASKED_VENDOR_WEBGL) return SPOOFED_VENDOR;
    }
    return getParameterProto.call(this, param);
  };

  // Also patch WebGL2
  if (typeof WebGL2RenderingContext !== 'undefined') {
    const getParameter2Proto = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function (param) {
      const ext = this.getExtension('WEBGL_debug_renderer_info');
      if (ext) {
        if (param === ext.UNMASKED_RENDERER_WEBGL) return SPOOFED_RENDERER;
        if (param === ext.UNMASKED_VENDOR_WEBGL) return SPOOFED_VENDOR;
      }
      return getParameter2Proto.call(this, param);
    };
  }
})();
"""

# ---------------------------------------------------------------------------
# 7. HTMLIFrameElement.prototype.contentWindow
# ---------------------------------------------------------------------------

IFRAME_CONTENTWINDOW_PATCH = """\
(() => {
  // Patch iframe contentWindow for cross-origin iframes.
  // Bot detectors check whether contentWindow is accessible.  When null
  // (cross-origin), return null -- never return the parent window as that
  // would let arbitrary code operate on the wrong browsing context.
  try {
    const descriptor = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    if (descriptor && descriptor.get) {
      const originalGetter = descriptor.get;
      Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get: function () {
          const result = originalGetter.call(this);
          if (result === null) {
            return null;
          }
          return result;
        },
        configurable: true,
      });
    }
  } catch (_) {}
})();
"""

# ---------------------------------------------------------------------------
# 8. MediaSource.isTypeSupported
# ---------------------------------------------------------------------------

MEDIA_CODECS_PATCH = """\
(() => {
  // Ensure common codecs report as supported (headless Chrome may lack codec support)
  if (typeof MediaSource !== 'undefined' && MediaSource.isTypeSupported) {
    const COMMON_CODECS = [
      'video/mp4; codecs="avc1.42E01E"',
      'video/mp4; codecs="avc1.42E01E, mp4a.40.2"',
      'video/webm; codecs="vp8"',
      'video/webm; codecs="vp8, vorbis"',
      'video/webm; codecs="vp9"',
      'audio/mp4; codecs="mp4a.40.2"',
      'audio/webm; codecs="opus"',
      'audio/webm; codecs="vorbis"',
    ];
    const originalIsTypeSupported = MediaSource.isTypeSupported.bind(MediaSource);
    MediaSource.isTypeSupported = function (mimeType) {
      if (COMMON_CODECS.some(c => mimeType === c)) {
        return true;
      }
      return originalIsTypeSupported(mimeType);
    };
  }
})();
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Ordered list mapping config flag names to their JS payloads
_PATCHES: list[tuple[str, str]] = [
	('webdriver_patch', WEBDRIVER_PATCH),
	('chrome_runtime_patch', CHROME_RUNTIME_PATCH),
	('plugins_patch', PLUGINS_PATCH),
	('languages_patch', LANGUAGES_PATCH),
	('permissions_patch', PERMISSIONS_PATCH),
	('webgl_patch', WEBGL_PATCH),
	('iframe_contentwindow_patch', IFRAME_CONTENTWINDOW_PATCH),
	('media_codecs_patch', MEDIA_CODECS_PATCH),
]


def get_stealth_scripts(config: 'StealthConfig') -> list[str]:
	"""Return the list of JS stealth scripts enabled by *config*.

	If ``config.enabled`` is False, an empty list is returned regardless of
	individual patch flags.
	"""
	if not config.enabled:
		return []
	return [script for flag, script in _PATCHES if getattr(config, flag, False)]
