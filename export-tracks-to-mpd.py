# Quod Libet plugin: export the current track list, or current selected tracks if any, to MPD.
#
# Adds two buttons to Quod Libet sidebar:
#   1. Add to MPD queue
#   2. Add to MPD playlist (playlist name : defined by MPD_STORED_PLAYLIST below)
# Moreover, checkbox "MPD clear" allows to clear the MPD queue or playlist before applying the addition

import os
import uuid

from gi.repository import Gtk
from mpd import MPDClient

from quodlibet import _, app
from quodlibet.plugins.events import EventPlugin
from quodlibet.util.library import get_scan_dirs


MPD_HOST = "127.0.0.1"
MPD_PORT = 6600
MPD_PASSWORD = None  # Set this if your MPD server requires a password.
MPD_STORED_PLAYLIST = "Filtered playlist"  # Playlist name
MPD_MAX_TRACKS = 500  # Max number of tracks allowed for exporting to MPD


def _music_root():
    """Return Quod Libet's first configured music scan directory."""
    scan_dirs = list(get_scan_dirs())
    if not scan_dirs:
        raise RuntimeError("Quod Libet has no configured music scan directories.")

    root = os.path.realpath(
        os.path.expanduser(os.fspath(scan_dirs[0]))
    )
    if not os.path.isdir(root):
        raise RuntimeError(
            f"Quod Libet's first music directory is unavailable: {root}"
        )

    return root


def _mpd_playlist_names(client):
    return {
        row["playlist"]
        for row in client.listplaylists()
        if "playlist" in row
    }


def _check_mpd_knows_tracks(client, uris):
    """Fail before changing a playlist or queue if a track is absent from MPD."""
    missing = []

    for uri in dict.fromkeys(uris):
        if not client.find("filename", uri):
            missing.append(uri)

    if missing:
        shown = "\n".join(missing[:12])
        if len(missing) > 12:
            shown += f"\n…and {len(missing) - 12} more"
        raise RuntimeError(
            "MPD could not find these files in its library:\n" + shown
        )


def _replace_stored_playlist(client, uris):
    """Clear the target stored playlist, write the new tracks, and verify it."""
    target = MPD_STORED_PLAYLIST

    if target in _mpd_playlist_names(client):
        client.playlistclear(target)

    for uri in uris:
        client.playlistadd(target, uri)


def _replace_current_queue(client, uris):
    # Validate before clearing so a path/database mismatch preserves the queue.
    _check_mpd_knows_tracks(client, uris)

    client.clear()
    for uri in uris:
        client.add(uri)


def _show_message(plugin, title, body, message_type=Gtk.MessageType.INFO):
    dialog = Gtk.MessageDialog(
        getattr(plugin, "plugin_window", None),
        Gtk.DialogFlags.MODAL,
        message_type,
        Gtk.ButtonsType.OK,
        title,
    )
    dialog.format_secondary_text(body)
    dialog.run()
    dialog.destroy()


def _song_uris(songs):
    """Convert tracks to MPD paths relative to Quod Libet's first scan directory."""
    root = _music_root()
    uris = []

    for song in songs:
        filename = song.get("~filename") if hasattr(song, "get") else None
        if not filename:
            continue

        filename = os.path.realpath(
            os.path.expanduser(os.fspath(filename))
        )

        # Reject tracks outside the configured root instead of producing ../../ paths.
        if os.path.commonpath((root, filename)) != root:
            raise RuntimeError(
                "This track is outside Quod Libet's first music scan directory.\n"
                f"Track: {filename}\n"
                f"Scan directory: {root}"
            )

        relative = os.path.relpath(filename, root)
        if relative == ".":
            continue

        uris.append(relative.replace(os.sep, "/"))

    if not uris:
        raise RuntimeError("The selected source contains no local audio files.")

    return uris


def _active_filter_songs():
    """Return selected tracks, or the full currently displayed list."""
    if app.window is None:
        raise RuntimeError("Quod Libet's main window is not available.")

    songlist = app.window.songlist
    selected = list(songlist.get_selected_songs())

    if selected:
        return selected

    return list(songlist.get_songs())


def _export(plugin, songs, source_name, replace_queue):
    client = None

    try:
        uris = _song_uris(songs)

        if len(uris) > MPD_MAX_TRACKS:
            raise RuntimeError(
                f"Export refused: {len(uris)} tracks exceeds the limit of "
                f"{MPD_MAX_TRACKS} (limit defined in the plugin settings)."
            )

        client = MPDClient()
        client.timeout = 5
        client.connect(MPD_HOST, MPD_PORT)

        if MPD_PASSWORD:
            client.password(MPD_PASSWORD)

        if replace_queue:
            _replace_current_queue(client, uris)
            destination = "MPD's current queue"
        else:
            _check_mpd_knows_tracks(client, uris)
            _replace_stored_playlist(client, uris)
            destination = f'MPD stored playlist "{MPD_STORED_PLAYLIST}"'

        _show_message(
            plugin,
            "Tracks exported",
            f"Exported {len(uris)} tracks from {source_name} to {destination}.",
        )

    except Exception as error:
        _show_message(
            plugin,
            "MPD export failed",
            str(error),
            Gtk.MessageType.ERROR,
        )

    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass


class ExportToMpdToolbar(EventPlugin):
    PLUGIN_ID = "export_tracks_to_mpd"
    PLUGIN_NAME = _("Export to MPD")
    PLUGIN_DESC = _("Add or replace current tracks (or current selected tracks) to MPD queue or playlist.")

    def enabled(self):
        if app.window is None:
            raise RuntimeError("Quod Libet's main window is not available.")

        self._toolbar = getattr(app.window, "top_bar", None)
        if self._toolbar is None:
            raise RuntimeError("Could not find Quod Libet's main toolbar.")

        self._toolbar_items = []

        buttons = (
            (_("MPD queue add"), self._on_add_queue),
            (_("MPD playlist add"), self._on_add_playlist),
        )

        for label, callback in buttons:
            tool_item = Gtk.ToolItem()
            button = Gtk.Button.new_with_label(label)
            button.set_tooltip_text(label)
            button.connect("clicked", callback)

            tool_item.add(button)
            self._toolbar.insert(tool_item, -1)
            tool_item.show_all()
            self._toolbar_items.append(tool_item)

        # 'clear' checkbox (has no action of its own; its state is read when a button is clicked)
        clear_item = Gtk.ToolItem()
        self._clear_check = Gtk.CheckButton.new_with_label(_("MPD clear"))
        self._clear_check.set_tooltip_text(
            _("Clear the selected MPD destination before adding tracks")
        )
        clear_item.add(self._clear_check)
        self._toolbar.insert(clear_item, -1)
        clear_item.show_all()
        self._toolbar_items.append(clear_item)

    def disabled(self):
        for tool_item in getattr(self, "_toolbar_items", []):
            parent = tool_item.get_parent()
            if parent is not None:
                parent.remove(tool_item)

        self._toolbar_items = []
        self._toolbar = None

    def _perform_mpd_action(self, operation):
        client = None

        try:
            client = MPDClient()
            client.timeout = 5
            client.connect(MPD_HOST, MPD_PORT)

            if MPD_PASSWORD:
                client.password(MPD_PASSWORD)

            message = operation(client)
            _show_message(self, "MPD action complete", message)

        except Exception as error:
            _show_message(
                self,
                "MPD action failed",
                str(error),
                Gtk.MessageType.ERROR,
            )

        finally:
            if client is not None:
                try:
                    client.disconnect()
                except Exception:
                    pass

    def _get_export_uris(self):
        uris = _song_uris(_active_filter_songs())

        if len(uris) > MPD_MAX_TRACKS:
            raise RuntimeError(
                f"Export refused: {len(uris)} tracks exceeds the limit of "
                f"{MPD_MAX_TRACKS}."
            )

        return uris

    def _add_to_stored_playlist(self, client, clear_first):
        target = MPD_STORED_PLAYLIST
        uris = self._get_export_uris()
        _check_mpd_knows_tracks(client, uris)

        if clear_first and target in _mpd_playlist_names(client):
            client.playlistclear(target)

        for uri in uris:
            client.playlistadd(target, uri)

        action = "Cleared and added" if clear_first else "Added"
        return f'{action} {len(uris)} tracks to MPD playlist "{target}".'


    def _add_to_mpd_queue(self, client, clear_first):
        uris = self._get_export_uris()
        _check_mpd_knows_tracks(client, uris)

        if clear_first:
            client.clear()

        for uri in uris:
            client.add(uri)

        action = "Cleared and added" if clear_first else "Added"
        return f"{action} {len(uris)} tracks to the MPD queue."

    def _on_add_playlist(self, _button):
        clear_first = self._clear_check.get_active()
        if clear_first:
            self._clear_check.set_active(False)

        self._perform_mpd_action(
            lambda client: self._add_to_stored_playlist(client, clear_first)
        )

    def _on_add_queue(self, _button):
        clear_first = self._clear_check.get_active()
        if clear_first:
            self._clear_check.set_active(False)

        self._perform_mpd_action(
            lambda client: self._add_to_mpd_queue(client, clear_first)
        )

