# Presentation Graphic Stream (PGS) Format Reference

**Based on:** [US Patent US 20090185789 A1](https://encrypted.google.com/patents/US20090185789)
**Source:** [TheScorpius666 blog post](https://blog.thescorpius.com/index.php/2017/07/15/presentation-graphic-stream-sup-files-bluray-subtitle-format/) (July 2017, updated October 2019)

---

The Presentation Graphic Stream (PGS) specification is used to show subtitles in
Blu-ray movies. When a PGS subtitle stream is ripped from a Blu-ray disc it is
usually saved in a file with the `.sup` extension (Subtitle Presentation).

A PGS is made of functional segments one after another. These segments share a
common header followed by a type-specific payload.

---

## Segment Header

Every segment begins with this 13-byte header:

| Name         | Size (bytes) | Description                                                         |
|--------------|:------------:|---------------------------------------------------------------------|
| Magic Number | 2            | `"PG"` (0x5047)                                                    |
| PTS          | 4            | Presentation Timestamp (90 kHz clock)                               |
| DTS          | 4            | Decoding Timestamp (always 0 in practice)                           |
| Segment Type | 1            | 0x14: PDS, 0x15: ODS, 0x16: PCS, 0x17: WDS, 0x80: END             |
| Segment Size | 2            | Size of the payload that follows                                    |

**Timestamps** have 90 kHz accuracy. To convert PTS to milliseconds, divide the
value by 90. For example, PTS `0x0004C11C` = 311,580 / 90 = **3,462 ms**.

DTS is always zero in practice and can be ignored.

---

## Segment Types

There are five segment types:

| Code | Name                                  | Abbreviation |
|:----:|---------------------------------------|:------------:|
| 0x16 | Presentation Composition Segment      | PCS          |
| 0x17 | Window Definition Segment             | WDS          |
| 0x14 | Palette Definition Segment            | PDS          |
| 0x15 | Object Definition Segment             | ODS          |
| 0x80 | End of Display Set Segment            | END          |

---

## Display Sets

A **Display Set (DS)** is a complete sub-picture definition, structured as:

```
PCS → WDS → PDS → ODS → END
```

A DS may contain multiple WDS, PDS, and ODS segments. The PCS opens the set and
the END segment closes it.

---

## Presentation Composition Segment (PCS) — 0x16

The PCS (also called the *Control Segment*) defines a new display composition.

| Name                         | Size (bytes) | Description                                                        |
|------------------------------|:------------:|--------------------------------------------------------------------|
| Width                        | 2            | Video width in pixels (e.g. 0x0780 = 1920)                        |
| Height                       | 2            | Video height in pixels (e.g. 0x0438 = 1080)                       |
| Frame Rate                   | 1            | Always 0x10. Can be ignored.                                      |
| Composition Number           | 2            | Incremented by one for every graphics update                       |
| Composition State            | 1            | 0x00: Normal, 0x40: Acquisition Point, 0x80: Epoch Start          |
| Palette Update Flag          | 1            | 0x00: False, 0x80: True (palette-only update)                     |
| Palette ID                   | 1            | ID of the palette used in a palette-only update                    |
| Number of Composition Objects| 1            | Number of composition objects in this segment                      |

### Composition States

- **Epoch Start (0x80):** Defines a *new display*. Contains all segments needed
  to show a new composition from scratch.
- **Acquisition Point (0x40):** Defines a *display refresh*. Used to compose in
  the middle of an Epoch. Includes new objects that replace old objects with the
  same Object ID.
- **Normal (0x00):** Defines a *display update*. Contains only segments with
  elements that differ from the preceding composition. Commonly used to clear
  the screen (Number of Composition Objects = 0) or to add new objects.

### Composition Object (repeats per object)

| Name                               | Size (bytes) | Description                                                |
|------------------------------------|:------------:|------------------------------------------------------------|
| Object ID                          | 2            | ID of the ODS that defines the image to show               |
| Window ID                          | 1            | ID of the WDS window the image is allocated to             |
| Object Cropped Flag                | 1            | 0x40: Force cropped display, 0x00: Off                     |
| Object Horizontal Position         | 2            | X offset from top-left of screen                           |
| Object Vertical Position           | 2            | Y offset from top-left of screen                           |
| Object Cropping Horizontal Position| 2            | X crop offset (only when cropped flag = 0x40)              |
| Object Cropping Vertical Position  | 2            | Y crop offset (only when cropped flag = 0x40)              |
| Object Cropping Width              | 2            | Crop width (only when cropped flag = 0x40)                 |
| Object Cropping Height             | 2            | Crop height (only when cropped flag = 0x40)                |

Cropping is used to progressively reveal a subtitle (e.g. showing a few words
first, then the rest).

> **Note:** Up to 2 objects can be shown simultaneously per PCS, though PGS
> supports up to 64 presentation objects in one epoch.

---

## Window Definition Segment (WDS) — 0x17

Defines one or more rectangular screen areas (*windows*) where sub-pictures are
drawn. Fields from Window ID through Window Height repeat for each window.

| Name                       | Size (bytes) | Description                            |
|----------------------------|:------------:|----------------------------------------|
| Number of Windows          | 1            | Number of windows defined              |
| Window ID                  | 1            | ID of this window                      |
| Window Horizontal Position | 2            | X offset from top-left of screen       |
| Window Vertical Position   | 2            | Y offset from top-left of screen       |
| Window Width               | 2            | Width of the window                    |
| Window Height              | 2            | Height of the window                   |

---

## Palette Definition Segment (PDS) — 0x14

Defines a colour palette. The last five fields repeat for each palette entry.

| Name                       | Size (bytes) | Description                            |
|----------------------------|:------------:|----------------------------------------|
| Palette ID                 | 1            | ID of the palette                      |
| Palette Version Number     | 1            | Version within the Epoch               |
| Palette Entry ID           | 1            | Entry number (0–255)                   |
| Luminance (Y)              | 1            | Y value                                |
| Colour Difference Red (Cr) | 1            | Cr value                               |
| Colour Difference Blue (Cb)| 1            | Cb value                               |
| Transparency (Alpha)       | 1            | Alpha value (0 = fully transparent)    |

> **Note:** The PGS byte order is **Y, Cr, Cb** — not Y, Cb, Cr.

---

## Object Definition Segment (ODS) — 0x15

Defines a graphics object (a rendered subtitle image on a transparent
background). The image data is compressed with Run-Length Encoding (RLE).

| Name                   | Size (bytes) | Description                                               |
|------------------------|:------------:|-----------------------------------------------------------|
| Object ID              | 2            | ID of this object                                         |
| Object Version Number  | 1            | Version of this object                                    |
| Last in Sequence Flag  | 1            | 0x40: Last, 0x80: First, 0xC0: First and last             |
| Object Data Length     | 3            | Length of RLE data **including** the 4 bytes for Width+Height |
| Width                  | 2            | Width of the image                                        |
| Height                 | 2            | Height of the image                                       |
| Object Data            | variable     | RLE-compressed image data (length = Object Data Length − 4)|

> **Note:** Object Data Length includes the 4 bytes for Width and Height.
> The actual RLE data size is Object Data Length minus 4.

Large objects may be split across multiple ODS fragments. The sequence flag
indicates whether the segment is the first, last, or only fragment.

### RLE Encoding

The image data uses Run-Length Encoding as defined in
[US Patent 7912305 B1](https://www.google.com/patents/US7912305):

| Byte Pattern                                     | Meaning                                    |
|--------------------------------------------------|--------------------------------------------|
| `CCCCCCCC`                                       | One pixel in colour C                      |
| `00000000 00LLLLLL`                               | L pixels in colour 0 (L: 1–63)            |
| `00000000 01LLLLLL LLLLLLLL`                      | L pixels in colour 0 (L: 64–16383)        |
| `00000000 10LLLLLL CCCCCCCC`                      | L pixels in colour C (L: 3–63)            |
| `00000000 11LLLLLL LLLLLLLL CCCCCCCC`             | L pixels in colour C (L: 64–16383)        |
| `00000000 00000000`                               | End of line                                |

---

## End of Display Set Segment (END) — 0x80

Always has a segment size of zero. Marks the end of a Display Set.

---

## Worked Example

A complete Display Set from a real `.sup` file:

```
00348a10  50 47 05 88 fd ec 00 00  00 00 16 00 13 07 80 04  |PG..............|
00348a20  38 10 01 ae 80 00 00 01  00 00 00 00 03 05 00 6c  |8..............l|
00348a30  50 47 05 88 fd ec 00 00  00 00 17 00 13 02 00 03  |PG..............|
00348a40  05 00 6c 01 79 00 2b 01  02 e3 03 a0 01 d8 00 2b  |..l.y.+........+|
00348a50  50 47 05 88 fd ec 00 00  00 00 14 00 9d 00 00 00  |PG..............|
00348a60  10 80 80 00 01 10 80 80  ff 02 1f 80 80 ff 03 2d  |...............-|
        ...
0034acc0  0f 11 00 86 12 00 0e 00  00 50 47 05 88 fd ec 00  |.........PG.....|
0034acd0  00 00 00 80 00 00                                 |......|
```

### Decoded Segments

**1. PCS** (offset 0x00348a10)

| Field                       | Value                      |
|-----------------------------|----------------------------|
| PTS                         | 17:11.822 (92,863,980 / 90)|
| Composition Number          | 430                        |
| Composition State           | Epoch Start (0x80)         |
| Palette Update              | False                      |
| Palette ID                  | 0                          |
| Composition Objects         | 1                          |
| → Object ID                | 0                          |
| → Window ID                | 0                          |
| → Position                 | (773, 108)                 |

**2. WDS** (offset 0x00348a30)

| Field                  | Value               |
|------------------------|----------------------|
| Number of Windows      | 2                    |
| Window 0 Position      | (773, 108)           |
| Window 0 Size          | 377 × 43             |
| Window 1 Position      | (739, 928)           |
| Window 1 Size          | 472 × 43             |

**3. PDS** (offset 0x00348a50)

- Palette ID: 0, Version: 0
- 31 palette entries

**4. ODS** (offset 0x00348afa)

| Field                  | Value                          |
|------------------------|--------------------------------|
| Object ID              | 0                              |
| Sequence Flag          | First and last (0xC0)          |
| Object Data Length     | 0x0021BB bytes                 |
| Image Size             | 377 × 43                       |

**5. END** (offset 0x0034acc9)

- Segment Size: 0

This Display Set shows a 377×43 image at screen position (773, 108) starting at
17 minutes 11.822 seconds.
