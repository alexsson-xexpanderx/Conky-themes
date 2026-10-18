// ConkyTray's window watcher, run inside KWin by tray.py. It sends the open
// windows, and where conky's panels are, to tray.py over D-Bus whenever
// either changes. KWin scripts cannot be called from outside, so the
// actions go the other way: tray.py loads a one-off script for each.

function info(w) {
    return {
        id: w.internalId.toString(),
        caption: w.caption,
        cls: w.resourceClass,
        name: w.resourceName,
        desktop: w.desktopFileName,
        pid: w.pid,
        minimized: w.minimized,
        active: w === workspace.activeWindow,
        attention: w.demandsAttention
    };
}

function isPanel(w) {
    return String(w.resourceClass).indexOf("conky") === 0 || String(w.resourceName).indexOf("conky") === 0;
}

function send(gone) {
    var windows = [], panels = [];
    workspace.windowList().forEach(function (w) {
        if (w === gone) return;
        if (w.normalWindow && !w.skipTaskbar) windows.push(info(w));
    });
    workspace.stackingOrder.forEach(function (w) {
        if (w === gone || !isPanel(w)) return;
        var g = w.frameGeometry;
        panels.push({pid: w.pid, x: g.x, y: g.y, w: g.width, h: g.height});
    });
    callDBus("org.conkytray", "/Tray", "org.conkytray.Tray", "Windows",
             JSON.stringify({windows: windows, panels: panels}));
}

function watch(w) {
    w.captionChanged.connect(function () { send(); });
    w.minimizedChanged.connect(function () { send(); });
    w.demandsAttentionChanged.connect(function () { send(); });
    if (w.desktopFileNameChanged) w.desktopFileNameChanged.connect(function () { send(); });
    if (isPanel(w)) w.frameGeometryChanged.connect(function () { send(); });
}

workspace.windowList().forEach(watch);
workspace.stackingOrder.forEach(function (w) { if (isPanel(w)) watch(w); });
workspace.windowAdded.connect(function (w) { watch(w); send(); });
workspace.windowRemoved.connect(function (w) { send(w); });
workspace.windowActivated.connect(function () { send(); });
send();
