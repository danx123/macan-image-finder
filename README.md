## 🐅 Macan Image Finder (Search by Image)
Macan Image Finder is a powerful, desktop application built with Python and PySide6 that enables content-based image retrieval (CBIR). It allows users to search for similar images within their indexed local directories using an image as a query, relying on robust computer vision techniques (ORB feature detection, feature matching, and homography estimation) to find matches even if the target image is scaled, rotated, or partially obscured.
---
## ✨ Key Features
Content-Based Image Retrieval (CBIR): Search for images based on their visual content, not just metadata or file names.
ORB Feature Detection: Utilizes OpenCV's Oriented FAST and Rotated BRIEF (ORB) algorithm for fast and efficient feature detection and description.
Robust Matching: Employs Brute-Force Matching with the Ratio Test (Lowe's Test) and Homography Estimation via RANSAC to filter out bad matches and verify geometric similarity, ensuring accurate results against variations in perspective, rotation, and scale.
Persistent Indexing: Stores image features (keypoints and descriptors) in an SQLite database for fast lookup, preventing the need to re-scan directories on subsequent searches.
Directory Management: Easily add, re-index, or remove specific directories from the index.
Multi-threaded Performance: Indexing and searching operations run in separate threads (using QThread), ensuring the application's GUI remains responsive during long processes.

---
## 📸 Screenshot
<img width="894" height="693" alt="Screenshot 2025-10-18 060440" src="https://github.com/user-attachments/assets/24346f23-096c-41a0-8f50-2f4775a8170a" />
---
## 📝 Changelog v2.2.0
- Update Framework

---
## 🚀 Usage
1. Indexing Directories
The first step is to build a feature index for your image library.
Click the "Add Directory to Index" button.
Select the root folder containing the images you want to search.
The application will start the indexing process, showing progress in the dedicated status bar and progress bar.

📝 Note: The features are stored persistently in an SQLite file (image_index.db) located in your application's data directory (e.g., %LOCALAPPDATA%/MacanImageViewer on Windows).

2. Managing Indexes
Use the "Manage Indexes" button to:
View all currently indexed directories.
Re-index a directory (useful if images have been significantly modified).
Remove a directory and all its associated features from the index.
Use "Clear All Indexes" to completely delete the database and start over.

3. Searching by Image
Click the "Select Query Image" button.
Choose an image you want to search for. The image will be displayed on the left.
The application will automatically start the search in a separate thread.
Results will populate the right panel, sorted by the number of inliers (a measure of geometric accuracy), along with a similarity score.
Result Display
Description
Filename
The name of the matching file.
Inliers
The count of robust matches found by the RANSAC algorithm.
Score
The ratio of RANSAC inliers to the total number of good matches (a confidence metric).

4. Result Actions
Right-click any result in the list to select "Open File Location", which will open the containing folder of the image in your file explorer.

## 📄 License
This project is licensed under the MIT License - see the LICENSE file (if applicable) for details.

## 🙏 Acknowledgements
OpenCV for providing robust and high-performance computer vision libraries.
Qt/PySide6 for the powerful and easy-to-use GUI framework.
