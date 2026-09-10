# Document parsing fixtures

These files are small hand-written stand-ins (a few hundred bytes to ~1KB
each), not saved copies of real regulator pages. They exercise the
markdownify/html2text extraction code paths but do not guarantee the parser
handles the messier markup real BaFin/ESMA/FCA pages ship (nested layout
divs, cookie banners, tracking scripts, etc.). Passing tests here is not a
guarantee of real-page compatibility — real-page breakage would surface via
the collector's own error/health monitoring instead.
