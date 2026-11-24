"""Render the application's shuttlecock mark to the Windows icon format."""
import sys
from PIL import Image, ImageDraw

canvas = Image.new("RGBA", (288, 288))
draw = ImageDraw.Draw(canvas)
draw.rounded_rectangle((0, 0, 287, 287), 68, fill="#123b49")
draw.polygon([(156, 54), (108, 144), (144, 180), (234, 132)], fill="#91e5cb")
draw.line([(156, 54), (144, 180)], fill="#234e56", width=10)
draw.line([(198, 90), (108, 144)], fill="#234e56", width=10)
draw.line([(102, 156), (86, 174)], fill="white", width=34)
draw.ellipse((67, 158, 101, 192), fill="white")
canvas.save(sys.argv[1], sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
