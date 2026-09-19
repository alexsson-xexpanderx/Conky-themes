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

In file lua_widgets.lua you can enable/disable graphic card temperature, set the temperature a full bar represents (max_temperature), or change colors, transparency and position. See lua_widgets.lua in USER CONFIGURATION section.

Temperatures are read straight from <i>/sys/class/hwmon</i>, so nothing is shelled out while conky runs. Steps (1)-(4) still matter because they load the kernel modules that publish those sensors, but the <i>sensors</i> command itself is no longer called.

<b>Size</b>

The conky is centred on the screen (<i>alignment = 'middle_middle'</i> in start_conky). Change that setting to move it elsewhere, or nudge it with x_rel_pos / y_rel_pos in lua_widgets.lua.

Two settings control how big the conky is. <i>widget_size</i> in lua_widgets.lua is the diameter in pixels of the outermost ring (default 450) - raise it to make everything bigger. <i>size</i> at the top of start_conky is the window the widget is drawn into (default 1000).

The rings also grow on their own as you add temperature bars: the bars wrap into evenly spaced rows and the rings expand until the block of them fits inside. Up to six bars the original layout is unchanged; 32 bars become two rows of 16 and need roughly a 1000px window. If the window is too small everything is scaled down together rather than clipped, and conky logs the size to set.

Cores are taken in the order the kernel reports them, whatever their numbering (a hybrid CPU labels them Core 0, Core 4, Core 8, ...), so number_of_physical_CPU_cores simply decides how many bars to draw.

The graphic card temperature comes from the amdgpu, nvidia or radeon hwmon entry, falling back to <i>nvidia-smi</i> only if the driver publishes no sensor. No card model needs to be configured.

A sensor that is missing or unreadable now shows as an empty bar instead of blanking the whole conky.
