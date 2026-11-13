`astronaut.jpg` is a cut-out of NASA's portrait of astronaut Eileen Collins, a work of the
United States government and in the public domain (the same picture ships with scikit-image
as `data.astronaut()`). The tests use it to try the face finder on a real face.

`klein.heic` is a drawn picture (a colour gradient with a bright corner, 96 × 64), saved as HEIC by
macOS (`sips -s format heic`). It is built the way a phone builds its files – with a turn field –
and is there to try reading, writing and turning HEIC.

`klein.mov` is a second of ffmpeg's test pattern (64 × 48, H.264), with the identifier and the date
an iPhone writes into the short film of a Live Photo, set by exiftool. It is there to try reading
films and keeping a Live Photo and its film together.
