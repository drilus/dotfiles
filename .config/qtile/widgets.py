from __future__ import division

from libqtile.widget import base, Volume, ThermalSensor, Battery, GroupBox, TaskList
from libqtile.log_utils import logger
from libqtile import bar, hook
import feedparser
from urllib import urlencode
import urllib2
import json
from subprocess import Popen, PIPE
from re import findall
# import dbus
import time
import iwlib
from weakref import proxy
from redobject import RedObject
from cal import get_next_event
from utils import NonBlockingSpawn
import os
import cairocffi


class RSS(base.ThreadedPollText):
    """
    Displays newest article from pravda.com.ua
    """
    orientations = base.ORIENTATION_HORIZONTAL

    def __init__(self, **config):
        config['update_interval'] = 0.25
        config['font'] = 'DejaVu Sans Mono'
        self.last_entry = None
        self.pos = -1
        base.ThreadedPollText.__init__(self, **config)

    def button_press(self, x, y, button):
        if self.last_entry is not None:
            pass

    def _update(self):
        feed = feedparser.parse('http://www.pravda.com.ua/rss/view_mainnews/')
        entry = feed.entries[0]
        self.last_entry = entry

    def poll(self):
        if self.last_entry is None or self.pos >= len(self.last_entry.title) + 2:
            self._update()
            self.pos = -1
        self.pos += 1
        return (self.last_entry.title + ' | ' + self.last_entry.title)[self.pos:self.pos + 40]


class KBLayout(base.ThreadedPollText):
    """
    Shows current keyboard layout.
    """
    orientations = base.ORIENTATION_HORIZONTAL

    def __init__(self, **config):
        config['update_interval'] = 1
        base.ThreadedPollText.__init__(self, **config)

    def button_press(self, x, y, button):
        pass

    def poll(self):
        # f0ac
        out, err = Popen(['xkblayout-state', 'print', '%s'], stdout=PIPE, stderr=PIPE).communicate()
        return u'\uf11d {}'.format(out.strip().upper())


class Ping(base._TextBox, NonBlockingSpawn):
    """
    Displays current Wi-Fi ESSID & ICMP latency.
    """
    orientations = base.ORIENTATION_HORIZONTAL

    def __init__(self, **config):
        self.ping = '?'
        self.last_text = ''
        self.wlan_name = '-'
        base._TextBox.__init__(self, **config)

    def _configure(self, *args, **kwargs):
        base._TextBox._configure(self, *args, **kwargs)
        self.do_ping()

    def do_ping(self):
        self.spawn(self._do_ping, self.on_ping_result)

    def _do_ping(self):
        iwconfig = iwlib.get_iwconfig('wlp3s0')
        is_connected = iwconfig['Access Point'] != '00:00:00:00:00:00'
        if is_connected:
            wlan_name = iwconfig['ESSID']
        else:
            wlan_name = 'Offline'

        out, err = Popen(['ping', '-c', '1', '8.8.8.8'], stdout=PIPE, stderr=PIPE).communicate()
        try:
            ping = int(float(findall('icmp_seq=[\d]+ ttl=[\d]+ time=([\d\.]+)', out)[0]))
            if ping > 999:
                ping = 999
        except:
            ping = None

        return (wlan_name, ping)

    def on_ping_result(self, result):
        wlan_name, ping = result

        self.update(wlan_name, ping)

        self.timeout_add(2, self.do_ping)

    def button_press(self, x, y, button):
        pass

    def update(self, wlan_name, ping):
        # \uf1eb
        if ping is None:
            ping_str = '???'
            self.foreground = '#FF0000'
        else:
            if ping > 100:
                self.foreground = '#FF0000'
            elif ping < 50:
                self.foreground = '#00FF00'
            else:
                factor = float(ping - 50) / 50
                self.foreground = '#%02x%02x00' % (factor * 127 + 128, (1 - factor) * 127 + 128)

            ping_str = str(ping).rjust(3, ' ')

        self.text = u'\uf072  {}: {}ms'.format(wlan_name, ping_str)

        if len(self.text) != len(self.last_text):
            self.bar.draw()
        else:
            self.draw()
        self.last_text = self.text


class OpenWeatherMap(base.ThreadedPollText):
    """
    Displays weather from openweathermap.org
    """
    orientations = base.ORIENTATION_HORIZONTAL

    ABS_ZERO = 273.15
    URL = 'http://api.openweathermap.org/data/2.5/weather'

    def __init__(self, **config):
        self.url = None
        for key in ('appid', 'location'):
            if key not in config:
                logger.error('Missing "{}" config parameter!'.format(key))
                return
        config['update_interval'] = 300
        self.url = OpenWeatherMap.URL + '?' + urlencode(dict(
            q=config.pop('location'),
            appid=config.pop('appid')
        ))
        base.ThreadedPollText.__init__(self, **config)

    def poll(self):
        if self.url is None:
            return 'N/A'
        try:
            response = json.loads(urllib2.urlopen(self.url).read())
            return u'\uF0C2  {name} {temp}\u00B0C'.format(
                city=response['name'],
                temp=int(response['main']['temp'] - OpenWeatherMap.ABS_ZERO),
                name=response['weather'][0]['main']
            )
        except Exception as e:
            logger.exception(e.message)
            return 'ERROR'


class NowPlayingWidget(base._TextBox):
    """
    Displays current song from VKPlayer.
    https://github.com/and3rson/vkplayer

    Also uses my own IPC implementation.
    """
    orientations = base.ORIENTATION_HORIZONTAL

    class VKPlayer(RedObject):
        def __init__(self, widget):
            logger.error('VKPlayer.__init__()')
            self.widget = proxy(widget)
            super(NowPlayingWidget.VKPlayer, self).__init__('org.dunai.vkplayer', logger)

        def on_connected_handler(self):
            logger.error('VKPlayer.on_connected_handler()')
            self.broadcast('request_state')

    def __init__(self, **config):
        self.is_downloading = False
        self.is_playing = False
        self.current_icon = '?'
        self.current_song = 'Empty'
        self.last_scroll = 0
        try:
            self.max_len = 40
            self.prev_len = 0
            self.shifted = 0
            self.sep = '  ***  '
            base._TextBox.__init__(self, **config)

            self.vkplayer = NowPlayingWidget.VKPlayer(self)
        except Exception as e:
            logger.exception(e.message)

    def _configure(self, *args):
        super(NowPlayingWidget, self)._configure(*args)
        self.timeout_add(0.2, self._shift)

    def timer_setup(self):
        def on_done(future):
            try:
                event = future.result()
            except Exception:
                self._update_state(False, False, 'Player is offline')
                logger.exception('next_event() raised exceptions')
            else:
                if event is not None:
                    if event.name == 'state_changed':
                        self._update_state(*event.data)
                    else:
                        logger.error('Unknown event received: {}'.format(event))
            self.timer_setup()

        future = self.qtile.run_in_executor(self.vkplayer.next_event)
        future.add_done_callback(on_done)

    def _shift(self, *args):
        self.timeout_add(0.2, self._shift)
        self._draw()
        self.shifted += 1
        if (self.shifted >= len(self.current_song) + len(self.sep)) or (len(self.current_song) != self.prev_len):
            self.shifted = 0
        self.prev_len = len(self.current_song)

    def _update_state(self, is_downloading, is_playing, current_song):
        try:
            self.is_downloading = is_downloading
            self.is_playing = is_playing

            self.current_icon = u'\uF019' if is_downloading else u'\uF04B' if is_playing else u'\uF04C'
            # self.current_icon = u'v' if is_downloading else u'>' if is_playing else u'x'

            current_song = current_song.decode('utf-8')
            self.current_song = current_song

            self._draw()
        except Exception as e:
            logger.exception(e.message)

    def _draw(self, redraw=False):
        if len(self.current_song) > self.max_len:
            shifted_title = self.sep.join([self.current_song] * 10)[self.shifted:self.shifted + self.max_len + 3]
        else:
            shifted_title = self.current_song.ljust(self.max_len + 3)
        self.text = u'{}  {}'.format(self.current_icon, shifted_title)
        if self.is_downloading:
            self.foreground = '#9999EE'
        elif self.is_playing:
            self.foreground = '#99EE99'
        else:
            self.foreground = '#EEEE99'

        self.draw()

    def _debounce(self):
        last_scroll = self.last_scroll
        self.last_scroll = time.time()

        if time.time() - last_scroll > 0.35:
            return True
        else:
            return False

    def button_press(self, x, y, button):
        if button == 1:
            # Left: play/pause
            self.vkplayer.broadcast('play_pause')

        elif button == 2:
            # Middle: random next
            self.vkplayer.broadcast('play_random')

        elif button == 4:
            # Scroll up
            if self._debounce():
                self.vkplayer.broadcast('play_prev')

        elif button == 5:
            # Scroll down
            if self._debounce():
                self.vkplayer.broadcast('play_next')


class Volume2(Volume):
    """
    Patched version of Volume widget that shows icon font chars.
    Psst: I use nerd-fonts package for this.
    """
    def _update_drawer(self):
        if self.theme_path:
            self.drawer.clear(self.background or self.bar.background)
            if self.volume <= 0:
                img_name = 'audio-volume-muted'
            elif self.volume <= 30:
                img_name = 'audio-volume-low'
            elif self.volume < 80:
                img_name = 'audio-volume-medium'
            else:  # self.volume >= 80:
                img_name = 'audio-volume-high'

            self.drawer.ctx.set_source(self.surfaces[img_name])
            self.drawer.ctx.paint()
        elif self.emoji:
            if self.volume <= 0:
                self.text = u'\U0001f507'
            elif self.volume <= 30:
                self.text = u'\U0001f508'
            elif self.volume < 80:
                self.text = u'\U0001f509'
            elif self.volume >= 80:
                self.text = u'\U0001f50a'
        else:
            if self.volume == -1:
                self.text = u'\uf026    M '
            else:
                self.text = u'\uf028 %s%%' % self.volume


class ThermalSensor2(ThermalSensor):
    """
    Patched version of ThermalSensor widget that shows icon font chars.
    Psst: I use nerd-fonts package for this.
    """
    def poll(self):
        # logger.error('{} {}'.format(self.foreground_normal, self.foreground_alert))
        temp_values = self.get_temp_sensors()
        if temp_values is None:
            return False
        text = u"\uf069 "
        if self.show_tag and self.tag_sensor is not None:
            text = self.tag_sensor + u": "
        parts = temp_values.get(self.tag_sensor, ['N/A'])
        parts = [x.decode('utf-8') for x in parts]
        text += u"".join(parts)
        temp_value = float(temp_values.get(self.tag_sensor, [0])[0])
        if temp_value > self.threshold:
            self.layout.colour = self.foreground_alert
        else:
            self.layout.colour = self.foreground_normal
        return text


class Battery2(Battery):
    """
    Patched version of Battery widget that shows icon font chars.
    Psst: I use nerd-fonts package for this.
    """
    def update(self):
        ntext = u'\uf0e7 {}'.format(self._get_text())
        if ntext != self.text:
            self.text = ntext
            self.bar.draw()


class UnreadMail(base._TextBox):
    """
    Displays count of unread messages in ThunderBird.
    Requires "Unread Count" plugin.

    https://addons.mozilla.org/en-US/thunderbird/addon/unread-count/?src=api

    Psst: I use nerd-fonts package for this.
    """
    orientations = base.ORIENTATION_HORIZONTAL

    def __init__(self, **config):
        self.unread_count = 0
        self.last_text = ''
        base._TextBox.__init__(self, **config)

    def timer_setup(self):
        self._update()

    def button_press(self, x, y, button):
        if button == 1:
            self.qtile.currentScreen.setGroup(self.qtile.groupMap['m'])

    def _update(self):
        profiles_file = open(os.path.expanduser('~/.thunderbird/profiles.ini'), 'r')
        for line in profiles_file.read().split('\n'):
            key, _, value = line.strip().partition('=')
            if key.lower() == 'path':
                profile = value.strip()
        profiles_file.close()

        self.unread_count = 0

        unread_file = open(os.path.expanduser('~/.thunderbird/{}/unread-counts'.format(profile)))
        for line in unread_file.read().split('\n'):
            count, _, box = line.strip().partition(':')
            try:
                self.unread_count += int(count)
            except ValueError:
                pass
        unread_file.close()

        self.timeout_add(0, self._ready)

    def _ready(self):
        # \uf1eb
        self.text = u'\uf003  {}'.format(self.unread_count)
        self.foreground = '#AAAAAA' if self.unread_count == 0 else '#F05040'
        if len(self.text) != len(self.last_text):
            self.bar.draw()
        else:
            self.draw()
        self.last_text = self.text
        self.timeout_add(15, self._update)


class NextEvent(base._TextBox, NonBlockingSpawn):
    """
    Displays time to next event in Google Calendar.
    Psst: I use nerd-fonts package for this.
    """
    orientations = base.ORIENTATION_HORIZONTAL

    def __init__(self, **config):
        self.last_text = ''
        base._TextBox.__init__(self, **config)

    def _configure(self, qtile, bar):
        base._TextBox._configure(self, qtile, bar)
        self.do_update()

    def button_press(self, x, y, button):
        pass
        # if button == 1:
        #     self.qtile.currentScreen.setGroup(self.qtile.groupMap['m'])

    def do_update(self):
        self.spawn(get_next_event, self.on_update_result)
        self.timeout_add(60, self.do_update)

    def on_update_result(self, result):
        # \uf1eb
        self.text = u'\uf073  {}'.format(result)
        self.foreground = '#00FFDD'
        if len(self.text) != len(self.last_text):
            self.bar.draw()
        else:
            self.draw()
        self.last_text = self.text


class GroupBox2(GroupBox):
    """
    A patched version of GroupBox that shows info
    # about active groups of each monitor.
    """
    defaults = GroupBox.defaults + [
        (
            "other_current_screen_border",
            "215578",
            "Border or line colour for current group on other screen."
        ),
    ]

    NUMBERS = {
        1: u'\u0323',
        2: u'\u0324',
        # 1: u'\u0329',
        # 2: u'\u0348',
        # 1: u'\u2071',
        # 2: u'\u00B2',
        3: u'\u00B3',
        4: u'\u2074',
        5: u'\u2075',
        6: u'\u2076',
        7: u'\u2077',
        8: u'\u2078',
        9: u'\u2079',
    }

    def get_group_text(self, group):
        window_count = len(group.windows)

        # group_name = group.name.upper()
        group_name = group.name

        if window_count:
            return u'{} {}'.format(
                group_name,
                u'\u2071' * window_count
                # GroupBox2.NUMBERS.get(window_count)
                # '+' * window_count
                # self.NUMBERS.get(window_count, self.NUMBERS.get(9) + '+')
            )
        else:
            return group_name

    def box_width(self, groups):
        width, height = self.drawer.max_layout_size(
            [self.get_group_text(i) for i in groups],
            self.font,
            self.fontsize
        )
        width += 6
        return width + self.padding_x * 2 + self.margin_x * 2 + \
            self.borderwidth * 2

    def draw(self):
        self.drawer.clear(self.background or self.bar.background)

        offset = 0
        for i, g in enumerate(self.groups):
            to_highlight = False

            is_block = False
            is_line = False

            bw = self.box_width([g])

            if self.group_has_urgent(g) and self.urgent_alert_method == "text":
                text_color = self.urgent_text
            elif g.windows:
                text_color = self.active
            else:
                text_color = self.inactive

            if g.screen:
                if self.highlight_method == 'text':
                    border = self.bar.background
                    text_color = self.this_current_screen_border
                else:
                    if self.bar.screen.group.name == g.name:
                        # This is active group
                        if self.qtile.currentScreen == self.bar.screen:
                            # Current screen
                            border = self.this_screen_border
                            to_highlight = True
                            is_block = True
                        else:
                            # Other screen
                            border = self.other_current_screen_border
                            is_line = False
                    else:
                        # This is inactive group
                        if self.qtile.currentScreen != self.bar.screen:
                            # Other screen
                            border = self.other_screen_border
                            is_line = False
                        else:
                            # Current screen
                            border = self.this_current_screen_border
                            to_highlight = True
                            is_block = True
            elif self.group_has_urgent(g) and \
                    self.urgent_alert_method in ('border', 'block', 'line'):
                border = self.urgent_border
                if self.urgent_alert_method == 'block':
                    is_block = True
                elif self.urgent_alert_method == 'line':
                    is_line = True
            else:
                border = self.background or self.bar.background

            self.drawbox(
                self.margin_x + offset,
                self.get_group_text(g),
                border,
                text_color,
                highlight_color=self.highlight_color,
                width=bw - self.margin_x * 2 - self.padding_x * 2,
                rounded=self.rounded,
                block=is_block,
                line=is_line,
                highlighted=to_highlight
            )
            offset += bw
        self.drawer.draw(offsetx=self.offset, width=self.width)


class TaskList2(base._Widget, base.PaddingMixin, base.MarginMixin):
    """
    A patch version of TaskList widget that fixes icon positions.
    """
    orientations = base.ORIENTATION_HORIZONTAL
    defaults = [
        ("font", "Arial", "Default font"),
        ("fontsize", None, "Font size. Calculated if None."),
        ("foreground", "ffffff", "Foreground colour"),
        (
            "fontshadow",
            None,
            "font shadow color, default is None(no shadow)"
        ),
        ("borderwidth", 2, "Current group border width"),
        ("border", "215578", "Border colour"),
        ("rounded", True, "To round or not to round borders"),
        (
            "highlight_method",
            "border",
            "Method of highlighting (one of 'border' or 'block') "
            "Uses \*_border color settings"
        ),
        ("urgent_border", "FF0000", "Urgent border color"),
        (
            "urgent_alert_method",
            "border",
            "Method for alerting you of WM urgent "
            "hints (one of 'border' or 'text')"
        ),
        ("max_title_width", 200, "size in pixels of task title")
    ]

    def __init__(self, **config):
        base._Widget.__init__(self, bar.STRETCH, **config)
        self.add_defaults(TaskList.defaults)
        self.add_defaults(base.PaddingMixin.defaults)
        self.add_defaults(base.MarginMixin.defaults)
        self._icons_cache = {}

    def box_width(self, text):
        width, _ = self.drawer.max_layout_size(
            [text],
            self.font,
            self.fontsize
        )
        width = width + self.padding_x * 2 + \
            self.margin_x * 2 + self.borderwidth * 2
        if width > self.max_title_width:
            width = self.max_title_width
        return width

    def _configure(self, qtile, bar):
        base._Widget._configure(self, qtile, bar)
        self.icon_size = self.bar.height - (self.borderwidth + 2) * 2

        if self.fontsize is None:
            calc = self.bar.height - self.margin_y * 2 - \
                self.borderwidth * 2 - self.padding_y * 2
            self.fontsize = max(calc, 1)
        self.layout = self.drawer.textlayout(
            "",
            "ffffff",
            self.font,
            self.fontsize,
            self.fontshadow,
            wrap=False
        )
        self.setup_hooks()

    def update(self, window=None):
        group = self.bar.screen.group
        if not window or window and window.group is group:
            self.bar.draw()

    def remove_icon_cache(self, window):
        wid = window.window.wid
        if wid in self._icons_cache:
            self._icons_cache.pop(wid)

    def invalidate_cache(self, window):
        self.remove_icon_cache(window)
        self.update(window)

    def setup_hooks(self):
        hook.subscribe.window_name_change(self.update)
        hook.subscribe.focus_change(self.update)
        hook.subscribe.float_change(self.update)
        hook.subscribe.client_urgent_hint_changed(self.update)

        hook.subscribe.net_wm_icon_change(self.invalidate_cache)
        hook.subscribe.client_killed(self.remove_icon_cache)

    def drawtext(self, text, textcolor, width):
        self.layout.text = text
        self.layout.font_family = self.font
        self.layout.font_size = self.fontsize
        self.layout.colour = textcolor
        if width is not None:
            self.layout.width = width

    def drawbox(self, offset, text, bordercolor, textcolor,
                width=None, rounded=False, block=False, icon=None):
        self.drawtext(text, textcolor, width)

        icon_padding = (self.icon_size + 2) if icon else 0
        padding_x = [self.padding_x + icon_padding, self.padding_x]

        framed = self.layout.framed(
            self.borderwidth,
            bordercolor,
            padding_x,
            self.padding_y
        )
        if block:
            framed.draw_fill(offset, self.margin_y, rounded)
        else:
            framed.draw(offset, self.margin_y, rounded)

        if icon:
            self.draw_icon(icon, offset)

    def get_clicked(self, x, y):
        window = None
        new_width = width = 0
        for w in self.bar.screen.group.windows:
            new_width += self.icon_size + self.box_width(w.name)
            if width <= x <= new_width:
                window = w
                break
            width = new_width
        return window

    def button_press(self, x, y, button):
        window = None
        current_win = self.bar.screen.group.currentWindow

        # TODO: support scroll
        if button == 1:
            window = self.get_clicked(x, y)

        if window and window is not current_win:
            window.group.focus(window, False)
            if window.floating:
                window.cmd_bring_to_front()
        elif window:
            window.toggle_minimize()

    def get_window_icon(self, window):
        if not window.icons:
            return None

        cache = self._icons_cache.get(window.window.wid)
        if cache:
            return cache

        icons = sorted(
            iter(window.icons.items()),
            key=lambda x: abs(self.icon_size - int(x[0].split("x")[0]))
        )
        icon = icons[0]
        width, height = map(int, icon[0].split("x"))

        img = cairocffi.ImageSurface.create_for_data(
            icon[1],
            cairocffi.FORMAT_ARGB32,
            width,
            height
        )

        surface = cairocffi.SurfacePattern(img)

        scaler = cairocffi.Matrix()

        if height != self.icon_size:
            sp = height / self.icon_size
            height = self.icon_size
            width /= sp
            scaler.scale(sp, sp)
        surface.set_matrix(scaler)
        self._icons_cache[window.window.wid] = surface
        return surface

    def draw_icon(self, surface, offset):
        if not surface:
            return

        x = offset + self.padding_x + self.borderwidth - 5 + self.margin_x
        y = self.padding_y + self.borderwidth - 2

        self.drawer.ctx.save()
        self.drawer.ctx.translate(x, y)
        self.drawer.ctx.set_source(surface)
        self.drawer.ctx.paint()
        self.drawer.ctx.restore()

    def draw(self):
        self.drawer.clear(self.background or self.bar.background)
        offset = 0

        for w in self.bar.screen.group.windows:
            state = ''
            if w is None:
                pass
            elif w.maximized:
                state = '[] '
            elif w.minimized:
                state = '_ '
            elif w.floating:
                state = 'V '

            task = "%s%s" % (state, w.name if w and w.name else " ")

            if w.urgent:
                border = self.urgent_border
                text_color = border
            elif w is w.group.currentWindow:
                border = self.border
                text_color = border
            else:
                border = self.background or self.bar.background
                text_color = self.foreground

            if self.highlight_method == 'text':
                border = self.bar.background
            else:
                text_color = self.foreground

            bw = self.box_width(task)
            self.drawbox(
                self.margin_x + offset,
                task,
                border,
                text_color,
                rounded=self.rounded,
                block=(self.highlight_method == 'block'),
                width=(bw - self.margin_x * 2 - self.padding_x * 2),
                icon=self.get_window_icon(w),
            )

            offset += bw + self.icon_size
        self.drawer.draw(offsetx=self.offset, width=self.width)


class ArchLogo(base._TextBox):
    """
    Displays a cute Arch Linux icon.
    """
    orientations = base.ORIENTATION_HORIZONTAL

    defaults = [
        ('scale', 1, 'Scale factor, defaults to 1'),
    ]

    def __init__(self, **config):
        base._TextBox.__init__(self, "", **config)
        self.add_defaults(ArchLogo.defaults)
        self.scale = 1.0 / self.scale

        self.length_type = bar.STATIC
        self.length = 0

    def _configure(self, qtile, bar):
        base._TextBox._configure(self, qtile, bar)
        self.text = ''
        self.surface = None
        self.setup_images()

    def draw(self):
        self.drawer.clear(self.background or self.bar.background)
        self.drawer.ctx.set_source(self.surface)
        self.drawer.ctx.paint()
        self.drawer.draw(offsetx=self.offset, width=self.length)

    def setup_images(self):
        img = cairocffi.ImageSurface.create_from_png(os.path.expanduser('~/.icons/arch-white.png'))

        input_width = img.get_width()
        input_height = img.get_height()

        sp = float(input_height) / (self.bar.height - 1)

        width = float(input_width) / sp
        if width > self.length:
            self.length = int(width) + self.actual_padding * 2

        imgpat = cairocffi.SurfacePattern(img)

        scaler = cairocffi.Matrix()

        scaler.scale(sp, sp)
        scaler.scale(self.scale, self.scale)
        factor = (1 - 1 / self.scale) / 2
        scaler.translate(-width * factor, -width * factor)
        scaler.translate(self.actual_padding * -1, 0)
        imgpat.set_matrix(scaler)

        imgpat.set_filter(cairocffi.FILTER_BEST)
        self.surface = imgpat


class DiskUsage(base._TextBox, NonBlockingSpawn):
    """
    Displays dist usage info.
    """
    orientations = base.ORIENTATION_HORIZONTAL

    def __init__(self, **config):
        base._TextBox.__init__(self, **config)

    def _configure(self, *args, **kwargs):
        base._TextBox._configure(self, *args, **kwargs)
        self.do_process()

    def do_process(self):
        self.spawn(self._do_process, self.on_process_result)

    def _do_process(self):
        statvfs = os.statvfs(self.root)

        return (
            statvfs.f_frsize * statvfs.f_blocks,  # Size of filesystem in bytes
            statvfs.f_frsize * statvfs.f_bfree,  # Actual number of free bytes
            statvfs.f_frsize * statvfs.f_bavail  # Number of free bytes that ordinary users
        )

    def on_process_result(self, result):
        size, _, free = result
        self.update(size, free)

        self.timeout_add(5, self.do_process)

    def button_press(self, x, y, button):
        pass

    def sizeof_fmt(self, num, suffix='B'):
        for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
            if abs(num) < 1024.0:
                return "%3.1f%s%s" % (num, unit, suffix)
            num /= 1024.0
        return "%.1f%s%s" % (num, 'Yi', suffix)

    def update(self, size, free):
        # \uf1eb
        free_factor = 1 - free / size
        self.foreground = '#%02x%02x00' % (free_factor * 127 + 128, (1 - free_factor) * 127 + 128)
        self.text = u'{}: {}/{} free'.format(self.root, self.sizeof_fmt(free), self.sizeof_fmt(size))
        self.bar.draw()
