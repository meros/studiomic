## services\.studiomic\.enable



Whether to enable studiomic, a clean, leveled virtual microphone with measured microphone choice\.



*Type:*
boolean



*Default:*

```nix
false
```



*Example:*

```nix
true
```



## services\.studiomic\.package



The DPDFNet LADSPA plugin\. Its ` defaultModel ` is overridden by ` model `\.



*Type:*
package



*Default:*

```nix
studiomic.packages.${system}.dpdfnet-ladspa
```



## services\.studiomic\.attenuationLimitDb

The most the model may remove, in dB\. 100 is the model’s full effect\.
A lower value mixes the original back in, which sounds more natural and
leaves more noise\.



*Type:*
integer or floating point number between 0 and 100 (both inclusive)



*Default:*

```nix
100
```



## services\.studiomic\.autoSelect\.enable



Choose the microphone by measurement\. ` studiomic-select ` opens every
microphone at once, scores what each recorded while somebody spoke,
and links the best one to the filter’s input\. It switches live, only
when another microphone is clearly better, only between phrases, and
not more often than ` dwellSeconds `\. A microphone that delivers digital
silence (muted, unplugged) or that heard none of the speech another one
heard is never chosen\. Every candidate microphone is open while it runs,
so a desktop may show the microphone as in use\. Off: WirePlumber links
the default source\.



*Type:*
boolean



*Default:*

```nix
true
```



## services\.studiomic\.autoSelect\.dwellSeconds



Least time between two switches\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
60
```



## services\.studiomic\.autoSelect\.intervalSeconds



Time between two measurements\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
5
```



## services\.studiomic\.autoSelect\.pattern



Regular expression of the PipeWire node names that may be chosen\.



*Type:*
string



*Default:*

```nix
"^(alsa_input|bluez_input)[.]"
```



## services\.studiomic\.description



Name that apps show for the virtual microphone\.



*Type:*
string



*Default:*

```nix
"Studiomic"
```



## services\.studiomic\.dynamics



Auto gain, de-esser, compressor and limiter after the denoiser\.



*Type:*
boolean



*Default:*

```nix
true
```



## services\.studiomic\.eq



Voice EQ after the high-pass (the bands in ` eqBands `)\.



*Type:*
boolean



*Default:*

```nix
true
```



## services\.studiomic\.eqBands



The EQ bands, applied in order when ` eq ` is on\. The default is fitted, third-octave
band by band, to an offline reference that a listener rated best (docs/design\.md)\.
It was fitted on one voice and one microphone, so a close microphone that already
has proximity bass may want a smaller cut at 150 Hz\.



*Type:*
list of (submodule)



*Default:*

```nix
[
  {
    freq = 150.0;
    gain = -2.5;
    q = 0.7;
    type = "lowshelf";
  }
  {
    freq = 500.0;
    gain = 3.5;
    q = 1.0;
    type = "peaking";
  }
  {
    freq = 2000.0;
    gain = 2.5;
    q = 1.0;
    type = "peaking";
  }
  {
    freq = 3500.0;
    gain = 4.0;
    q = 1.0;
    type = "peaking";
  }
  {
    freq = 5500.0;
    gain = 1.5;
    q = 1.0;
    type = "peaking";
  }
  {
    freq = 9000.0;
    gain = -3.0;
    q = 0.7;
    type = "highshelf";
  }
]
```



## services\.studiomic\.eqBands\.\*\.freq



Centre or corner frequency in Hz\.



*Type:*
integer or floating point number between 20 and 20000 (both inclusive)



## services\.studiomic\.eqBands\.\*\.gain



Gain in dB\.



*Type:*
integer or floating point number between -24 and 24 (both inclusive)



## services\.studiomic\.eqBands\.\*\.q



Quality factor\.



*Type:*
positive integer or floating point number, meaning >0



## services\.studiomic\.eqBands\.\*\.type



Filter shape (RBJ audio-EQ cookbook biquad)\.



*Type:*
one of “lowshelf”, “peaking”, “highshelf”



## services\.studiomic\.makeDefault



Make the virtual microphone the default microphone when the service
starts\. WirePlumber remembers a microphone picked by hand and that choice
beats any priority, so the service sets the remembered default itself\.
Off: pick it in a sound panel, or run ` wpctl set-default <id> `\.



*Type:*
boolean



*Default:*

```nix
true
```



## services\.studiomic\.maxGainDb



The most the auto gain may raise a quiet microphone, in dB\.



*Type:*
integer or floating point number between 0 and 60 (both inclusive)



*Default:*

```nix
24
```



## services\.studiomic\.model



DPDFNet model\. The light one scored within 0\.05 DNSMOS of the quality one
on the keyboard, fan and chatter test\. On ONNX Runtime the quality one costs
2\.1 times the CPU (31\.8 s against 14\.8 s per 60 s of audio, Lunar Lake laptop,
one core)\. That is 53 % of a core on average, and single calls take longer than
the 10 ms hop in about 1 call in 20 (thread CPU time, quantum 480), so it can
overrun inside the real-time thread\. The light model never did (maximum 84 %
of the budget)\. The quality model therefore runs on Ceva’s native engine (about a
fifth of that CPU), which needs AVX2 and FMA\. Without them, or when the weights
file does not check out, the plugin runs the light model instead and says so in
the PipeWire client’s log\.



*Type:*
one of “dpdfnet2_48khz_hr”, “dpdfnet8_48khz_hr”



*Default:*

```nix
"dpdfnet8_48khz_hr"
```



## services\.studiomic\.nodeName



PipeWire node name of the virtual microphone\.



*Type:*
string



*Default:*

```nix
"studiomic"
```



## services\.studiomic\.priority



` priority.session ` of the virtual microphone\. Above every physical
microphone (the ladder ends at 2000), so it is preferred as the default\.
0 keeps the default on the hardware\.



*Type:*
signed integer



*Default:*

```nix
3000
```



## services\.studiomic\.selectorPackage



The microphone selector (` studiomic-select `)\.



*Type:*
package



*Default:*

```nix
studiomic.packages.${system}.studiomic-select
```



## services\.studiomic\.sourcePriorities



The ladder of physical microphones by kind of device, highest priority
first\. It decides before anything is measured, and when nothing can be
measured, because the distance from the mouth sets the signal-to-noise
ratio more than any other factor: a headset or desk microphone beats a
webcam, and a webcam beats a laptop array\. Later rules win when two match\.



*Type:*
list of (submodule)



*Default:*

```nix
[
  {
    match = "~alsa_input\\..*sof_sdw\\.pro-input-.*";
    priority = 500;
  }
  {
    match = "~bluez_input\\..*";
    monitor = "bluez";
    priority = 1500;
  }
  {
    match = "~alsa_input\\.usb-.*";
    priority = 1900;
  }
  {
    match = "~alsa_input\\.usb-.*(OBSBOT|Camera|Webcam|C920|C922|Brio).*";
    priority = 800;
  }
]
```



## services\.studiomic\.sourcePriorities\.\*\.key



The node property to match\.



*Type:*
string



*Default:*

```nix
"node.name"
```



## services\.studiomic\.sourcePriorities\.\*\.match



PipeWire match value; a leading ` ~ ` makes it a regular expression\.



*Type:*
string



## services\.studiomic\.sourcePriorities\.\*\.monitor



Which WirePlumber device monitor the node comes from\.



*Type:*
one of “alsa”, “bluez”



*Default:*

```nix
"alsa"
```



## services\.studiomic\.sourcePriorities\.\*\.priority



` priority.session ` for the matching nodes\.



*Type:*
signed integer



## services\.studiomic\.targetLufs



Loudness of speech after the auto gain, in LUFS\.



*Type:*
integer or floating point number between -60 and 0 (both inclusive)



*Default:*

```nix
-23
```


