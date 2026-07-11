import os
import shutil

# Use the directory where the script is located
directory = os.path.dirname(os.path.abspath(__file__))

# Get the script's own filename so we don't move it
script_name = os.path.basename(__file__)

# Loop through all files in the directory
for filename in os.listdir(directory):
    # Skip the script itself
    if filename == script_name:
        continue

    file_path = os.path.join(directory, filename)

    # Skip if it's already a directory
    if os.path.isfile(file_path):
        # Split filename and extension
        name, ext = os.path.splitext(filename)

        # Create a folder with the same name as the file (without extension)
        folder_path = os.path.join(directory, name)
        os.makedirs(folder_path, exist_ok=True)

        # Move the file into that folder
        new_file_path = os.path.join(folder_path, filename)
        shutil.move(file_path, new_file_path)

        print(f"Moved: {filename} -> {folder_path}")