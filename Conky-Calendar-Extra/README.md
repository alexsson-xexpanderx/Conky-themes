Conky

<b>HowTo:</b>

(1) Install the lm-sensors package.

(2) Run sudo sensors-detect and choose YES to all YES/no questions.

(3) At the end of sensors-detect, a list of modules that needs to be loaded will displayed. Type "yes" to have sensors-detect insert those modules into /etc/modules, or edit /etc/modules yourself. 

(4) In Ubuntu, run sudo service module-init-tools restart. This will read the changes you made to /etc/modules in step 3, and insert the new modules into the kernel.  Or, if using other distributions simply restart your computer.

(5) Set your number of physical cores in lua_widgets.lua  in USER CONFIGURATION section:
```
      number_of_physical_CPU_cores = YOUR_NUMBER_HERE
```
(6) Start conky with:
```
      conky -c start_conky
```

<b>Optionally:</b>

In file lua_widgets.lua you can enable/disable graphic card temperature, set the temperature a full bar represents (max_temperature), or change colors, transparency and position. The modernized variant adds warm_above: readings stay in the accent colour until that temperature and only shift towards HTML_warm above it, so an idle machine never looks hot. See lua_widgets.lua in USER CONFIGURATION section.

Temperatures are read straight from <i>/sys/class/hwmon</i>, so nothing is shelled out while conky runs. Steps (1)-(4) still matter because they load the kernel modules that publish those sensors, but the <i>sensors</i> command itself is no longer called.

<b>Size</b>

The conky is centred on the screen (<i>alignment = 'middle_middle'</i> in start_conky). Change that setting to move it elsewhere, or nudge it with x_rel_pos / y_rel_pos in lua_widgets.lua.

Two settings control how big the conky is. <i>widget_size</i> in lua_widgets.lua is the diameter in pixels of the outermost ring (default 450) - raise it to make everything bigger. <i>size</i> at the top of start_conky is the window the widget is drawn into (default 1000).

The rings also grow on their own as you add temperature bars: the bars wrap into evenly spaced rows and the rings expand until the block of them fits inside. Up to six bars the original layout is unchanged; 32 bars become two rows of 16 and need roughly a 1000px window. If the window is too small everything is scaled down together rather than clipped, and conky logs the size to set.

Cores are taken in the order the kernel reports them, whatever their numbering (a hybrid CPU labels them Core 0, Core 4, Core 8, ...), so number_of_physical_CPU_cores simply decides how many bars to draw.

The graphic card temperature comes from the amdgpu, nvidia or radeon hwmon entry, falling back to <i>nvidia-smi</i> only if the driver publishes no sensor. No card model needs to be configured.

In the modernized variant the GPU is not one more bar in the row: with enable_graphic_card_temperature_sensor set to Yes it gets its own dial beside the two filesystem dials, showing degrees rather than a percentage, its ring coloured by the same heat ramp as the bars. The two filesystem dials keep their spacing when it is off, and all three tuck in a little when it is on.

A sensor that is missing or unreadable now shows as an empty bar instead of blanking the whole conky.

<b>Modernized variant</b>

<i>lua_widgets_modernized.lua</i> and <i>start_conky_modernized</i> are a second, restyled copy of the same widget. Run it with:
```
      conky -c start_conky_modernized
```
It reads the same sensors and scales the same way, but redraws the dial: rounded tick rings for weeks and days, labelled arcs for months and weekdays that stay upright all the way round instead of hanging upside down at the bottom, a large clock with the date beneath it, and slim rounded temperature bars that shift from the accent colour towards HTML_warm as a core heats up. The filesystem gauges are progress rings with cairo-drawn icons in them - stacked platters for root, a house for home - with the used percentage underneath.

Colors are set by HTML_base, HTML_accent and HTML_warm, and the four opacity_* values control how strongly tracks, labels, readouts and live values are drawn. The original lua_widgets.lua is untouched, so both can be used side by side.
