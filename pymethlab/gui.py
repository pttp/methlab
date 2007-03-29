#  methlab - A music library application
#  Copyright (C) 2007 Ingmar K. Steen (iksteen@gmail.com)
#
LICENSE = '''
  This program is free software; you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation; either version 2 of the License, or
  (at your option) any later version.

  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.

  You should have received a copy of the GNU General Public License
  along with this program; if not, write to the Free Software
  Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
'''

import os
import gobject
import gtk
import gtk.glade
import pango
from ConfigParser import ConfigParser
from db import DB
from querytranslator import QueryTranslatorException
from drivers import DRIVERS, DummyDriver
from db import sqlite

# Case insensitive string compare
def case_insensitive_cmp(model, a, b):
  return cmp(model.get_value(a, 0).upper(), model.get_value(b, 0).upper())

# Quote and escape a string to be used in a query
def query_escape(s):
  return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'

# The main window
class MethLabWindow:
  CONFIG_PATH = '~/.methlab/config'
  # Generic options
  DEFAULT_DRIVER = DummyDriver.name
  DEFAULT_UPDATE_ON_STARTUP = True
  DEFAULT_SEARCH_ON_ARTIST_AND_ALBUM = True
  # User interface options
  DEFAULT_COLUMN_ORDER = 'path artist album track title year genre comment'
  DEFAULT_VISIBLE_COLUMNS = 'artist album track title'
  DEFAULT_ARTISTS_COLLAPSIBLE = False

  DEFAULT_CONFIG = {
    'options': {
      'driver': DEFAULT_DRIVER,
      'update_on_startup': `DEFAULT_UPDATE_ON_STARTUP`,
      'search_on_artist_and_album': `DEFAULT_SEARCH_ON_ARTIST_AND_ALBUM`,
    },
    'interface': {
      'column_order': DEFAULT_COLUMN_ORDER,
      'visible_columns': DEFAULT_VISIBLE_COLUMNS,
      'artists_collapsible': `DEFAULT_ARTISTS_COLLAPSIBLE`,
    }
  }

  def __init__(self):
    # Set the default configuration options
    self.config = ConfigParser()
    for section, options in self.DEFAULT_CONFIG.items():
      self.config.add_section(section)
      for key, value in options.items():
        self.config.set(section, key, value)
    # Merge configuration file with default options
    self.config.read(os.path.expanduser(self.CONFIG_PATH))

    # Create our database back-end
    self.db = DB()

    # If this value is not 0, searches will not occur
    self.inhibit_search = 1

    # Some timeout tags we may wish to cancel
    self.search_timeout_tag = None
    self.flash_timeout_tag = None

    # A tuple describing all the result columns (name, field, model_col)
    self.result_columns = \
    {
      'path': ('Path', 0),
      'artist': ('Artist', 1),
      'album': ('Album', 2),
      'track': ('#', 3),
      'title': ('Title', 4),
      'year': ('Year', 5),
      'genre': ('Genre', 6),
      'comment': ('Comment', 7)
    }

    # Load the gui from the XML file
    self.gladefile = os.path.join(os.path.split(__file__)[0], 'methlab.glade')
    wtree = gtk.glade.XML(self.gladefile)

    # Map the widgets from the wtree to the class
    for w in wtree.get_widget_prefix(''):
      setattr(self, w.name, w)

    # Create the audio-player back-end
    self.set_driver(self.config.get('options', 'driver'))

    # Build the menus
    self.build_menus()

    # Set up the search options
    self.set_active_search_fields()

    # Create a cell renderer we re-use
    cell_renderer = gtk.CellRendererText()

    # Set up the artists / albums model
    self.artists_albums_model = gtk.TreeStore(str)
    self.artists_albums_model.set_sort_func(0, case_insensitive_cmp)
    self.artists_albums_model.set_sort_column_id(0, gtk.SORT_ASCENDING)
    self.artist_iters = {}
    self.album_iters = {}
    self.update_artists_albums_model()

    # Set up the artists / albums tree view
    artist_album_renderer = gtk.CellRendererText()
    col = gtk.TreeViewColumn('Artist / Album', artist_album_renderer, text = 0)
    col.set_cell_data_func(artist_album_renderer, self.get_artists_albums_cell_data)
    self.tvArtistsAlbums.append_column(col)
    self.tvArtistsAlbums.set_model(self.artists_albums_model)
    self.update_artists_collapsible()
    self.tvArtistsAlbums.get_selection().connect('changed', self.on_artists_albums_selection_changed)
    self.tvArtistsAlbums.get_selection().set_mode(gtk.SELECTION_MULTIPLE)

    # Set up the saved searches model
    self.searches_model = gtk.ListStore(str, str, str)
    self.searches_model.set_sort_func(0, case_insensitive_cmp)
    self.searches_model.set_sort_column_id(0, gtk.SORT_ASCENDING)
    self.search_iters = {}
    self.update_searches_model()

    # Set up the saved searches tree view
    self.tvSearches.append_column(gtk.TreeViewColumn('Saved search', cell_renderer, text = 0))
    self.tvSearches.set_model(self.searches_model)
    self.tvSearches.get_selection().connect('changed', self.on_searches_selection_changed)
    self.tvSearches.connect('button-press-event', self.on_searches_button_press_event)

    # Set up the search history model
    self.history_model = gtk.ListStore(str)
    self.cbeSearch.set_model(self.history_model)

    # Set up the search results model
    self.results_model = gtk.ListStore(str, str, str, int, str, int, str, str)
    # 0: Path
    self.results_model.set_sort_func(0, case_insensitive_cmp)
    # 1: Artist
    self.results_model.set_sort_func(1, case_insensitive_cmp)
    # 2: Album
    self.results_model.set_sort_func(2, case_insensitive_cmp)
    # 3: Track#
    # 4: Title
    self.results_model.set_sort_func(4, case_insensitive_cmp)
    # 5: Year
    # 6: Genre
    self.results_model.set_sort_func(6, case_insensitive_cmp)
    # 7: Comment
    self.results_model.set_sort_func(7, case_insensitive_cmp)

    # Get the column order and do a sanity check
    column_order = self.config.get('interface', 'column_order').split(' ')
    a = column_order[:]
    a.sort()
    b = self.result_columns.keys()
    b.sort()
    if a != b:
      column_order = self.DEFAULT_COLUMN_ORDER.split(' ')

    # What fields does the user wish to see and do a sanity check
    visible_columns = self.config.get('interface', 'visible_columns').split(' ')
    visible_columns = [column for column in visible_columns if column in column_order]
    if not visible_columns:
      visible_columns = self.DEFAULT_VISIBLE_COLUMNS

    # Set up the results tree view
    for column_field in column_order:
      column_name, column_id = self.result_columns[column_field]
      column = gtk.TreeViewColumn(None, cell_renderer, text = column_id)
      column.field = column_field
      column.set_sizing(gtk.TREE_VIEW_COLUMN_AUTOSIZE)
      column.set_reorderable(True)
      column.set_visible(column_field in visible_columns)
      self.tvResults.append_column(column)
      # Haxory and trixory to be able to catch right click on the header
      column.set_clickable(True)
      column.set_widget(gtk.Label(column_name))
      column.get_widget().show()
      parent = column.get_widget().get_ancestor(gtk.Button)
      if parent:
        parent.connect('button-press-event', self.on_results_header_button_press_event)
    self.tvResults.get_selection().set_mode(gtk.SELECTION_MULTIPLE)
    self.tvResults.set_model(self.results_model)
    self.tvResults.connect('columns-changed', self.on_results_columns_changed)

    # Fix and hook up the expanders
    self.btnSearchOptions.connect('clicked', self.on_section_button_clicked)
    self.btnSearches.connect('clicked', self.on_section_button_clicked)
    self.btnArtistsAlbums.connect('clicked', self.on_section_button_clicked)
    self.btnArtistsAlbums.connect('button-press-event', self.on_artists_albums_button_press_event)

    # Hook up the search options
    self.cbSearchPath.connect('toggled', self.on_search_field_toggled)
    self.cbSearchArtist.connect('toggled', self.on_search_field_toggled)
    self.cbSearchAlbum.connect('toggled', self.on_search_field_toggled)
    self.cbSearchTitle.connect('toggled', self.on_search_field_toggled)
    self.cbSearchTrack.connect('toggled', self.on_search_field_toggled)
    self.cbSearchYear.connect('toggled', self.on_search_field_toggled)
    self.cbSearchGenre.connect('toggled', self.on_search_field_toggled)
    self.cbSearchComment.connect('toggled', self.on_search_field_toggled)

    # Hook up the search bar
    self.entSearch.connect('focus-in-event', self.on_search_focus_in_event)
    self.entSearch.connect('activate', self.on_search)
    self.entSearch.connect('changed', self.on_search_changed)

    # Hook up the buttons
    self.btnClearSearch.connect('clicked', self.on_clear_search)
    self.btnPlayResults.connect('clicked', self.on_play_results)
    self.btnEnqueueResults.connect('clicked', self.on_enqueue_results)
    self.btnSaveSearch.connect('clicked', self.on_save_search)

    # Set up accelerators
    accel_group = gtk.AccelGroup()
    self.window.add_accel_group(accel_group)
    self.entSearch.add_accelerator('grab-focus', accel_group, ord('f'), gtk.gdk.CONTROL_MASK, 0)
    self.btnClearSearch.add_accelerator('clicked', accel_group, ord('e'), gtk.gdk.CONTROL_MASK, 0)
    self.cbSearchPath.add_accelerator('activate', accel_group, ord('1'), gtk.gdk.MOD1_MASK, 0)
    self.cbSearchArtist.add_accelerator('activate', accel_group, ord('2'), gtk.gdk.MOD1_MASK, 0)
    self.cbSearchAlbum.add_accelerator('activate', accel_group, ord('3'), gtk.gdk.MOD1_MASK, 0)
    self.cbSearchTitle.add_accelerator('activate', accel_group, ord('4'), gtk.gdk.MOD1_MASK, 0)
    self.cbSearchTrack.add_accelerator('activate', accel_group, ord('5'), gtk.gdk.MOD1_MASK, 0)
    self.cbSearchYear.add_accelerator('activate', accel_group, ord('6'), gtk.gdk.MOD1_MASK, 0)
    self.cbSearchGenre.add_accelerator('activate', accel_group, ord('7'), gtk.gdk.MOD1_MASK, 0)
    self.cbSearchComment.add_accelerator('activate', accel_group, ord('8'), gtk.gdk.MOD1_MASK, 0)

    # Connect destroy signal and show the window
    self.window.connect('destroy', gtk.main_quit)
    self.window.resize(640, 380)
    self.window.show()

    # Haxory and trixory to prevent widgets from needlessly rearranging
    self.swSearches.realize()
    self.tvSearches.realize()
    self.swArtistsAlbums.realize()
    self.tvArtistsAlbums.realize()
    self.hpaned1.set_position(self.hpaned1.get_position())

    # Finished initializing
    self.inhibit_search = 0

    # Set focus to the search bar
    self.entSearch.grab_focus()

    # Start updating the library
    if self.config.getboolean('options', 'update_on_startup'):
      self.update_db()

  def build_menus(self):
    # Create the File menu
    self.filemenu = gtk.Menu()
    filemenu_item = gtk.MenuItem('_File')
    filemenu_item.set_submenu(self.filemenu)
    self.menubar.append(filemenu_item)

    # File -> Update library now
    self.filemenu_update = gtk.ImageMenuItem('_Update library now')
    self.filemenu_update.set_image(gtk.image_new_from_stock(gtk.STOCK_REFRESH, gtk.ICON_SIZE_MENU))
    self.filemenu_update.connect('activate', self.on_file_update)
    self.filemenu.append(self.filemenu_update)

    # Separator
    self.filemenu.append(gtk.SeparatorMenuItem())

    # File -> Exit
    self.filemenu_exit = gtk.ImageMenuItem(gtk.STOCK_QUIT)
    self.filemenu_exit.connect('activate', gtk.main_quit)
    self.filemenu.append(self.filemenu_exit)

    # Create the Settings menu
    self.settingsmenu = gtk.Menu()
    settingsmenu_item = gtk.MenuItem('_Settings')
    settingsmenu_item.set_submenu(self.settingsmenu)
    self.menubar.append(settingsmenu_item)

    # Settings -> Update on startup
    self.settingsmenu_update_on_startup = gtk.CheckMenuItem('_Update library on startup')
    self.settingsmenu_update_on_startup.set_active(self.config.getboolean('options', 'update_on_startup'))
    self.settingsmenu_update_on_startup.connect('toggled', self.on_settings_update_on_startup_toggled)
    self.settingsmenu.append(self.settingsmenu_update_on_startup)

    # Settings -> Directories
    self.settingsmenu_directories = gtk.ImageMenuItem('_Directories')
    self.settingsmenu_directories.set_image(gtk.image_new_from_stock(gtk.STOCK_DIRECTORY, gtk.ICON_SIZE_MENU))
    self.settingsmenu_directories.connect('activate', self.on_settings_directories)
    self.settingsmenu.append(self.settingsmenu_directories)

    # Settings -> Audio player
    self.drivermenu = gtk.Menu()
    drivermenu_item = gtk.MenuItem('_Audio player')
    drivermenu_item.set_submenu(self.drivermenu)
    self.settingsmenu.append(drivermenu_item)

    # Settings -> Driver -> <...>
    group = None
    for driver in DRIVERS:
      item = gtk.RadioMenuItem(group, driver.name)
      if group is None:
        group = item
      if self.ap_driver.__class__ == driver:
        item.set_active(True)
      item.connect('toggled', self.on_settings_driver_toggled, driver.name)
      self.drivermenu.append(item)

    # Create the Help menu
    self.helpmenu = gtk.Menu()
    helpmenu_item = gtk.MenuItem('_Help')
    helpmenu_item.set_submenu(self.helpmenu)
    self.menubar.append(helpmenu_item)

    # Help -> About
    self.helpmenu_about = gtk.ImageMenuItem(gtk.STOCK_ABOUT)
    self.helpmenu_about.connect('activate', self.on_about)
    self.helpmenu.append(self.helpmenu_about)

    # Show everything
    self.menubar.show_all()

  # Set which backend driver to use
  def set_driver(self, drivername):
    driver = DummyDriver
    for d in DRIVERS:
      if d.name == drivername:
        driver = d
        break
    try:
      self.ap_driver = driver()
    except Exception, e:
      msg = 'An error has occured while activating the selected driver.\n\nThe error is: %s\n\nFalling back to the dummy driver.' % str(e.message)
      dialog = gtk.MessageDialog(self.window, 
        flags = gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
        type = gtk.MESSAGE_ERROR, 
        buttons = (gtk.BUTTONS_OK),
        message_format = msg
      )
      dialog.run()
      dialog.destroy()
      self.ap_driver = DummyDriver()

  def set_config(self, section, option, value):
    if type(value) != str:
      value = `value`
    if not self.config.has_section(section):
      self.config.add_section(option)
    self.config.set(section, option, value)
    config_path = os.path.expanduser(self.CONFIG_PATH)
    config_dir = os.path.split(config_path)[0]
    if not os.path.exists(config_dir):
      os.path.makedirs(config_dir)
    self.config.write(open(config_path, 'w'))

  def get_artists_albums_cell_data(self, column, cell, model, iter):
    value = model.get_value(iter, 0)
    if not value:
      cell.set_property('style', pango.STYLE_ITALIC)
      if model.iter_parent(iter) is None:
        cell.set_property('text', 'unknown artist')
      else:
        cell.set_property('text', 'unknown album')
    else:
      cell.set_property('style', pango.STYLE_NORMAL)

  def update_artists_collapsible(self):
    collapsible = self.config.getboolean('interface', 'artists_collapsible')
    self.tvArtistsAlbums.set_enable_tree_lines(not collapsible)
    self.tvArtistsAlbums.set_property('show-expanders', collapsible)
    if collapsible:
      self.tvArtistsAlbums.set_property('level-indentation', 0)
    else:
      self.tvArtistsAlbums.set_property('level-indentation', 25)
      self.tvArtistsAlbums.expand_all()

  # Get a list of fields that are enabled
  def get_active_search_fields(self):
    fields = []
    if self.cbSearchPath.get_active():
      fields.append('path')
    if self.cbSearchArtist.get_active():
      fields.append('artist')
    if self.cbSearchAlbum.get_active():
      fields.append('album')
    if self.cbSearchTitle.get_active():
      fields.append('title')
    if self.cbSearchTrack.get_active():
      fields.append('track')
    if self.cbSearchYear.get_active():
      fields.append('year')
    if self.cbSearchGenre.get_active():
      fields.append('genre')
    if self.cbSearchComment.get_active():
      fields.append('comment')
    return fields

  # Set active fields (fields is a list or tuple)
  def set_active_search_fields(self, fields = ('artist', 'album', 'title')):
    self.inhibit_search += 1
    self.cbSearchPath.set_active('path' in fields)
    self.cbSearchArtist.set_active('artist' in fields)
    self.cbSearchAlbum.set_active('album' in fields)
    self.cbSearchTitle.set_active('title' in fields)
    self.cbSearchTrack.set_active('track' in fields)
    self.cbSearchYear.set_active('year' in fields)
    self.cbSearchGenre.set_active('genre' in fields)
    self.cbSearchComment.set_active('comment' in fields)
    self.inhibit_search -= 1
    self.search()

  def update_search_fields(self):
    fields = self.get_active_search_fields()
    self.db.set_search_fields(*fields)

  def update_artists_albums_model(self):
    artists = [row['artist'] for row in self.db.get_artists()]
    for artist in artists:
      if not self.artist_iters.has_key(artist):
        iter = self.artists_albums_model.append(None)
        self.artists_albums_model.set_value(iter, 0, artist)
        self.artist_iters[artist] = iter
    for artist, iter in self.artist_iters.items():
      if not artist in artists:
        self.artists_albums_model.remove(iter)
        del self.artist_iters[artist]
        for (artist_, album_), iter in self.album_iters.items():
          if artist_ == artist:
            del self.album_iters[(artist_, album_)]

    artists_albums = [(row['artist'], row['album']) for row in self.db.get_artists_albums()]
    for artist, album in artists_albums:
      if not self.album_iters.has_key((artist, album)):
        artist_iter = self.artist_iters[artist]
        iter = self.artists_albums_model.append(artist_iter)
        self.artists_albums_model.set_value(iter, 0, album)
        self.album_iters[(artist, album)] = iter
    for (artist, album), iter in self.album_iters.items():
      if not (artist, album) in artists_albums:
        self.artists_albums_model.remove(iter)
        del self.album_iters[(artist, album)]

  def update_searches_model(self):
    search_names = []
    searches = self.db.get_searches()
    for row in searches:
      name = row['name']
      query = row['query']
      fields = row['fields']
      search_names.append(name)
      iter = self.search_iters.get(name, None)
      if iter is None:
        iter = self.searches_model.append(None)
        self.searches_model.set(iter, 0, name, 1, query, 2, fields)
        self.search_iters[name] = iter
      else:
        self.searches_model.set(iter, 1, query, 2, fields)
    for search_name, iter in self.search_iters.items():
      if not search_name in search_names:
        self.searches_model.remove(iter)
        del self.search_iters[search_name]

  def cancel_search_timeout(self):
    if self.search_timeout_tag is not None:
      gobject.source_remove(self.search_timeout_tag)
      self.search_timeout_tag = None

  def search(self, add_to_history = False):
    self.cancel_search_timeout()
    if self.inhibit_search:
      return

    self.unflash_search_entry()

    self.results_model.clear()
    query = self.entSearch.get_text()
    if not query:
      return
    try:
      if query[0] == '@':
        results = self.db.query_tracks(query[1:])
      else:
        results = self.db.search_tracks(query)
    except sqlite.OperationalError:
      self.flash_search_entry()
      return
    except QueryTranslatorException:
      self.flash_search_entry()
      return

    if add_to_history:
      self.add_to_history(query)

    for result in results:
      iter = self.results_model.append()
      self.results_model.set(iter,
        0, result['path'],
        1, result['artist'],
        2, result['album'],
        3, result['track'],
        4, result['title'],
        5, result['year'],
        6, result['genre'],
        7, result['comment']
      )

  def cancel_flash_search_entry(self):
    if self.flash_timeout_tag is not None:
      gobject.source_remove(self.flash_timeout_tag)
      self.flash_timeout_tag = None

  def flash_search_entry(self):
    self.cancel_flash_search_entry()
    self.entSearch.modify_base(gtk.STATE_NORMAL, gtk.gdk.color_parse('#ff0000'))
    self.flash_timeout_tag = gobject.timeout_add(300, self.unflash_search_entry)

  def unflash_search_entry(self):
    self.cancel_flash_search_entry()
    self.entSearch.modify_base(gtk.STATE_NORMAL, None)
    return False

  def get_selected_result_paths(self):
    files = []
    sel = self.tvResults.get_selection()
    model, paths = sel.get_selected_rows()
    if not paths:
      iter = model.get_iter_first()
      while iter:
        files.append(model.get_value(iter, 0))
        iter = model.iter_next(iter)
    else:
      for path in paths:
        iter = model.get_iter(path)
        if iter:
          files.append(model.get_value(iter, 0))
    return files

  def update_db(self):
    def yield_func():
      gtk.main_iteration(False)

    dialog = gtk.Dialog \
    (
      'Please wait...',
      self.window,
      gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT
    )
    dialog.set_has_separator(False)
    dialog.vbox.pack_start(gtk.Label('Please wait while MethLab updates the library...'))
    dialog.connect('delete_event', lambda w, e: True)
    dialog.show_all()
    self.db.update(yield_func)
    dialog.destroy()
    self.update_artists_albums_model()
    if self.config.getboolean('interface', 'artists_collapsible'):
      self.tvArtistsAlbums.expand_all()
    self.search()

  def add_to_history(self, query):
    iter = self.history_model.get_iter_first()
    while iter:
      if self.history_model.get_value(iter, 0) == query:
        self.history_model.move_after(iter, None)
        break
      iter = self.history_model.iter_next(iter)
    else:
      iter = self.history_model.prepend()
      self.history_model.set_value(iter, 0, query)

    node = self.history_model.iter_nth_child(None, 49)
    next = None
    if node:
      next = self.history_model.iter_next(node)
    while next:
      self.history_model.remove(next)
      next = self.history_model.iter_next(node)

  def on_section_button_clicked(self, button):
    if button == self.btnSearchOptions:
      self.frSearchOptions.show()
      self.swSearches.hide()
      self.swArtistsAlbums.hide()
      self.entSearch.grab_focus()
    elif button == self.btnSearches:
      self.swSearches.show()
      self.frSearchOptions.hide()
      self.swArtistsAlbums.hide()
      self.tvSearches.realize()
      self.tvSearches.grab_focus()
    else:
      self.swArtistsAlbums.show()
      self.frSearchOptions.hide()
      self.swSearches.hide()
      self.tvArtistsAlbums.realize()
      self.tvArtistsAlbums.grab_focus()

  def on_artists_albums_button_press_event(self, widget, event):
    if event.button == 3:
      menu = gtk.Menu()
      # Collapsible artists menu item
      item = gtk.CheckMenuItem('Collapsible artists')
      item.set_active(self.config.getboolean('interface', 'artists_collapsible'))
      item.connect('toggled', self.on_artists_albums_popup_collapsible_artists_toggled)
      menu.append(item)
      # Search album on artist as well menu item
      item = gtk.CheckMenuItem('Search album on artist as well')
      item.set_active(self.config.getboolean('options', 'search_on_artist_and_album'))
      item.connect('toggled', self.on_artists_albums_popup_search_on_artist_and_album_toggled)
      menu.append(item)
      # Run the menu
      menu.show_all()
      menu.popup(None, None, None, event.button, event.time)
      return True

  def on_artists_albums_popup_collapsible_artists_toggled(self, menuitem):
    self.set_config('interface', 'artists_collapsible', menuitem.get_active())
    self.update_artists_collapsible()

  def on_artists_albums_popup_search_on_artist_and_album_toggled(self, menuitem):
    self.set_config('options', 'search_on_artist_and_album', menuitem.get_active())

  def on_artists_albums_selection_changed(self, selection):
    model, paths = selection.get_selected_rows()
    if not paths:
      return

    queries = []
    for path in paths:
      iter = model.get_iter(path)
      parent = model.iter_parent(iter)
      if parent is None:
        artist = query_escape(model.get_value(iter, 0))
        queries.append('(artist = %s)' % artist)
      else:
        album = query_escape(model.get_value(iter, 0))
        if self.config.getboolean('options', 'search_on_artist_and_album'):
          artist = query_escape(model.get_value(parent, 0))
          queries.append('(artist = %s AND album = %s)' % (artist, album))
        else:
          queries.append('(album = %s)' % album)

    self.entSearch.set_text('@' + ' OR '.join(queries))
    self.search()

  def on_search_focus_in_event(self, widget, event):
    self.btnSearchOptions.clicked()

  def on_search(self, entry):
    self.search(True)

  def on_search_changed(self, editable):
    self.cancel_search_timeout()
    if self.inhibit_search:
      return
    text = editable.get_text()
    if not text:
      self.search()
      return
    if text[0] == '@' or len(text) <= 3:
      return
    self.search_timeout_tag = gobject.timeout_add(500, self.on_search_timeout)

  def on_search_timeout(self):
    self.search_timeout_tag = None
    self.search(True)
    return False

  def on_results_header_button_press_event(self, widget, event):
    if event.button == 3:
      columns = self.tvResults.get_columns()
      visible_columns = [column for column in columns if column.get_visible()]
      one_visible_column = len(visible_columns) == 1
      menu = gtk.Menu()
      for column in columns:
        item = gtk.CheckMenuItem(column.get_widget().get_text())
        if column in visible_columns:
          item.set_active(True)
          if one_visible_column:
            item.set_sensitive(False)
        else:
          item.set_active(False)
        item.connect('activate', self.on_result_header_popup_activate, column)
        menu.append(item)
      menu.show_all()
      menu.popup(None, None, None, event.button, event.time)
      return True
    return False

  def on_result_header_popup_activate(self, menuitem, column):
    column.set_visible(not column.get_visible())
    visible_columns = ' '.join([column.field for column in self.tvResults.get_columns() if column.get_visible()])
    self.set_config('interface', 'visible_columns', visible_columns)

  def on_results_columns_changed(self, treeview):
    columns = treeview.get_columns()
    if len(columns) != len(self.result_columns):
      # Don't save the column order during destruction
      return
    column_order = ' '.join([column.field for column in columns])
    self.set_config('interface', 'column_order', column_order)

  def on_play_results(self, button):
    files = self.get_selected_result_paths()
    if files:
      self.ap_driver.play(files)

  def on_enqueue_results(self, button):
    files = self.get_selected_result_paths()
    if files:
      self.ap_driver.enqueue(files)

  def on_search_field_toggled(self, button):
    self.update_search_fields()
    text = self.entSearch.get_text()
    if text and text[0] != '@':
      self.search()

  def on_save_search(self, button):
    query = self.entSearch.get_text()
    if not query:
      return
    fields = self.get_active_search_fields()

    sel = self.tvSearches.get_selection()
    model, iter = sel.get_selected()
    if iter:
      name = model.get_value(iter, 0)
    else:
      if query[:1] == '@':
        name = query
      else:
        name = "'" + query + "' in " + ', '.join(fields)

    dialog = gtk.Dialog \
    (
      'Save search',
      self.window,
      gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
      (gtk.STOCK_CANCEL, gtk.RESPONSE_REJECT,
       gtk.STOCK_OK,     gtk.RESPONSE_ACCEPT)
    )
    dialog.vbox.pack_start(gtk.Label('Name of the search'))
    entry = gtk.Entry()
    entry.set_text(name)
    entry.connect('activate', lambda w: dialog.response(gtk.RESPONSE_ACCEPT))
    dialog.vbox.pack_start(entry)
    dialog.show_all()
    response = dialog.run()
    if response == gtk.RESPONSE_ACCEPT:
      name = entry.get_text()
      if name:
        self.db.add_search(name, query, ' '.join(fields))
        self.update_searches_model()
        self.tvSearches.get_selection().select_iter(self.search_iters[name])
        self.btnSearches.clicked()
    dialog.destroy()

  def on_searches_selection_changed(self, selection):
    model, iter = selection.get_selected()
    if iter is None:
      return
    self.inhibit_search += 1
    query, fields = model.get(iter, 1, 2)
    fields = fields.split()
    self.set_active_search_fields(fields)
    self.entSearch.set_text(query)
    self.inhibit_search -= 1
    self.search()

  def on_searches_button_press_event(self, treeview, event):
    if event.button == 3:
      data = treeview.get_path_at_pos(int(event.x), int(event.y))
      if data is not None:
        path, col, r_x, r_y = data
        iter = treeview.get_model().get_iter(path)
        name = treeview.get_model().get_value(iter, 0)
        treeview.get_selection().select_iter(iter)
        menu = gtk.Menu()
        item = gtk.MenuItem('Remove')
        item.connect('activate', self.on_searches_popup_remove, name)
        menu.append(item)
        menu.show_all()
        menu.popup(None, None, None, event.button, event.time)
      else:
        treeview.get_selection().unselect_all()
      return True

  def on_searches_popup_remove(self, menuitem, name):
    self.db.delete_search(name)
    self.tvSearches.get_model().remove(self.search_iters[name])
    del self.search_iters[name]

  def on_clear_search(self, button):
    self.entSearch.set_text('')
    self.entSearch.grab_focus()

  def on_file_update(self, menuitem):
    self.update_db()

  def on_settings_directories(self, menuitem):
    def on_add_directory(button, model):
      dialog = gtk.FileChooserDialog \
        (
          'Add directory',
          self.window,
          gtk.FILE_CHOOSER_ACTION_SELECT_FOLDER,
          (gtk.STOCK_CANCEL, gtk.RESPONSE_REJECT,
           gtk.STOCK_OK,     gtk.RESPONSE_ACCEPT)
        )
      if dialog.run() == gtk.RESPONSE_ACCEPT:
        dir = dialog.get_current_folder()
        iter = model.append(None)
        model.set_value(iter, 0, dir)
      dialog.destroy()

    def on_remove_directory(button, treeview):
      model, iter = treeview.get_selection().get_selected()
      if iter is not None:
        model.remove(iter)

    changed = False

    model = gtk.ListStore(str)
    roots = [os.path.abspath(root[0]) for root in self.db.get_roots()]
    for root in roots:
      iter = model.append(None)
      model.set_value(iter, 0, root)

    gladefile = os.path.join(os.path.split(__file__)[0], 'dirdialog.glade')
    wtree = gtk.glade.XML(gladefile)
    dialog = wtree.get_widget('dialog')
    treeview = wtree.get_widget('tvDirs')
    treeview.append_column(gtk.TreeViewColumn('Directory', gtk.CellRendererText(), text = 0))
    treeview.set_model(model)
    wtree.get_widget('btnAdd').connect('clicked', on_add_directory, model)
    wtree.get_widget('btnRemove').connect('clicked', on_remove_directory, treeview)
    wtree.get_widget('btnOk').connect('clicked', lambda w: dialog.response(gtk.RESPONSE_ACCEPT))
    wtree.get_widget('btnCancel').connect('clicked', lambda w: dialog.response(gtk.RESPONSE_REJECT))
    dialog.resize(300, 300)
    if dialog.run() == gtk.RESPONSE_ACCEPT:
      dirs = [row[0] for row in model]
      for root in roots:
        if not root in dirs:
          print 'Purging', root
          self.db.delete_root(root + '/')
          changed = True
      for dir in dirs:
        if not dir in roots:
          print 'Adding', dir
          self.db.add_root(dir)
          changed = True

    dialog.destroy()

    if changed:
      self.update_db()

  def on_settings_driver_toggled(self, menuitem, driver):
    if menuitem.get_active():
      self.set_driver(driver)
      self.set_config('options', 'driver', driver)

  def on_settings_update_on_startup_toggled(self, menuitem):
    self.set_config('options', 'update_on_startup', menuitem.get_active())

  def on_about(self, menuitem):
    dialog = gtk.AboutDialog()
    dialog.set_name('MethLab')
    dialog.set_version('0.0.0')
    dialog.set_copyright('MethLab is (C) 2007 Ingmar Steen.\nThe bundled xmmsalike library is (C) 2006 Ben Wolfson and Risto A. Paju')
    dialog.set_license(LICENSE)
    dialog.set_website('http://thegraveyard.org/')
    dialog.set_authors(['Ingmar Steen <iksteen@gmail.com> (Main developer)'])
    dialog.run()
    dialog.destroy()
