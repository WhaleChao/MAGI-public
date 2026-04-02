import sys
import os
from PIL import Image

def is_green(pixel, tolerance=50):
    r, g, b = pixel[:3]
    return g > 200 and r < tolerance and b < tolerance

def remove_green_screen(img):
    img = img.convert("RGBA")
    datas = img.getdata()
    
    # Check the top-left pixel to get the exact background color if it's perfectly solid
    bg_color = datas[0]
    
    newData = []
    for item in datas:
        # If it's pure green or close to it
        if is_green(item):
            newData.append((255, 255, 255, 0))
        else:
            newData.append(item)
            
    img.putdata(newData)
    return img

def create_gif(input_path, output_path, is_idle=False):
    # Load the 2x2 grid
    try:
        sheet = Image.open(input_path)
    except Exception as e:
        print(f"Error opening {input_path}: {e}")
        return

    w, h = sheet.size
    fw, fh = w // 2, h // 2
    
    frames = []
    # Extract 4 frames: Top-Left, Top-Right, Bottom-Left, Bottom-Right
    boxes = [
        (0, 0, fw, fh),
        (fw, 0, w, fh),
        (0, fh, fw, h),
        (fw, fh, w, h)
    ]
    
    for box in boxes:
        frame = sheet.crop(box)
        frame = remove_green_screen(frame)
        frames.append(frame)
        
    duration = 400 if is_idle else 150
        
    # Save as animated GIF
    frames[0].save(
        output_path,
        save_all=True,
        append_images=frames[1:],
        duration=duration,
        loop=0,
        disposal=2 # Clear frame before rendering next
    )
    print(f"Saved {output_path} successfully!")

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python process_sprites.py <input.png> <output.gif> <is_idle: true/false>")
        sys.exit(1)
        
    input_file = sys.argv[1]
    output_file = sys.argv[2]
    is_idle = sys.argv[3].lower() == 'true'
    
    create_gif(input_file, output_file, is_idle)
