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

// An app's own window: KWin's own windows (a title bar button's tooltip is
// one) count as normal windows too, but KWin does not manage them and they
// are pop-ups. Listed, the tooltip showed on the panel as an unknown app.
function isAppWindow(w) {
    return w.normalWindow && w.managed && !w.popupWindow;
}

function isPanel(w) {
    return String(w.resourceClass).indexOf("conky") === 0 || String(w.resourceName).indexOf("conky") === 0;
}

function send(gone) {
    var windows = [], panels = [];
    workspace.windowList().forEach(function (w) {
        if (w === gone) return;
        if (isAppWindow(w) && !w.skipTaskbar) windows.push(info(w));
    });
    workspace.stackingOrder.forEach(function (w) {
        if (w === gone || !isPanel(w)) return;
        var g = w.frameGeometry;
        panels.push({pid: w.pid, x: g.x, y: g.y, w: g.width, h: g.height});
    });
    callDBus("org.conkytray", "/Tray", "org.conkytray.Tray", "Windows",
             JSON.stringify({windows: windows, panels: panels}));
}

function onDesktop(w) {
    if (w.onAllDesktops) return true;
    var desktops = w.desktops || [];
    for (var i = 0; i < desktops.length; i++) {
        if (desktops[i] === workspace.currentDesktop) return true;
    }
    return false;
}

// Which edges of the screen a window reaches (a maximised one, all four). A
// panel along an edge one reaches stops floating, as Plasma's does. Sent only
// when it changes: during a drag the geometry changes every frame.
var edgesSent = null;
function sendEdges(gone) {
    var reached = {};
    workspace.windowList().forEach(function (w) {
        if (w === gone || !isAppWindow(w) || w.minimized || isPanel(w) || !onDesktop(w)) return;
        var a = workspace.clientArea(KWin.MaximizeArea, w), g = w.frameGeometry;
        if (g.y + g.height >= a.y + a.height - 1) reached.bottom = true;
        if (g.y <= a.y + 1) reached.top = true;
        if (g.x <= a.x + 1) reached.left = true;
        if (g.x + g.width >= a.x + a.width - 1) reached.right = true;
    });
    var text = Object.keys(reached).sort().join(",");
    if (text === edgesSent) return;
    edgesSent = text;
    callDBus("org.conkytray", "/Tray", "org.conkytray.Tray", "Edges", text);
}

function watch(w) {
    // A title can change many times a second (a progress bar, a game's frame
    // rate); it sends just that title, not the whole list.
    w.captionChanged.connect(function () {
        callDBus("org.conkytray", "/Tray", "org.conkytray.Tray", "Caption",
                 w.internalId.toString(), w.caption);
    });
    w.minimizedChanged.connect(function () { send(); sendEdges(); });
    w.frameGeometryChanged.connect(function () { sendEdges(); });
    if (w.desktopsChanged) w.desktopsChanged.connect(function () { sendEdges(); });
    w.demandsAttentionChanged.connect(function () { send(); });
    if (w.desktopFileNameChanged) w.desktopFileNameChanged.connect(function () { send(); });
    if (isPanel(w)) w.frameGeometryChanged.connect(function () { send(); });
}

workspace.windowList().forEach(watch);
workspace.stackingOrder.forEach(function (w) { if (isPanel(w)) watch(w); });
workspace.windowAdded.connect(function (w) { watch(w); send(); sendEdges(); });
workspace.windowRemoved.connect(function (w) { send(w); sendEdges(w); });
workspace.windowActivated.connect(function () { send(); });
workspace.currentDesktopChanged.connect(function () { sendEdges(); });
send();
sendEdges();
