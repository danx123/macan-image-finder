import sys
import os
import cv2
import numpy as np
import sqlite3
import pickle
import subprocess
import platform
from pathlib import Path
from PySide6.QtWidgets import (
    QApplication, QWidget, QPushButton, QVBoxLayout, QHBoxLayout,
    QFileDialog, QLabel, QListWidget, QListWidgetItem, QProgressBar, QMessageBox, QMenu,
    QStatusBar, QDialog, QDialogButtonBox
)
from PySide6.QtGui import QPixmap, QIcon, QAction
from PySide6.QtCore import (
    Qt, QSize, QThread, Signal, QObject
)

# --- HELPER FUNCTIONS FOR KEYPOINT SERIALIZATION (REMAINS THE SAME) ---
def serialize_keypoints(keypoints):
    if keypoints is None:
        return None
    return pickle.dumps(
        [(kp.pt, kp.size, kp.angle, kp.response, kp.octave, kp.class_id) for kp in keypoints]
    )

def deserialize_keypoints(pickled_data):
    if pickled_data is None:
        return []
    data = pickle.loads(pickled_data)
    return [
        cv2.KeyPoint(x=p[0][0], y=p[0][1], size=p[1], angle=p[2], response=p[3], octave=p[4], class_id=p[5])
        for p in data
    ]

# --- SQLITE DATABASE MANAGER CLASS (UPDATED) ---
class DatabaseManager:
    def __init__(self, db_path="image_index.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.create_tables()

    def create_tables(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS features (
                    path TEXT PRIMARY KEY,
                    keypoints BLOB,
                    descriptors BLOB
                )
            """)
            # --- NEW: Table to store the list of indexed directories ---
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS indexed_directories (
                    path TEXT PRIMARY KEY
                )
            """)

    def add_or_update_features(self, path, keypoints, descriptors):
        kps_blob = serialize_keypoints(keypoints)
        des_blob = pickle.dumps(descriptors)
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO features (path, keypoints, descriptors) VALUES (?, ?, ?)",
                (path, kps_blob, des_blob)
            )

    # --- NEW: Functions to manage indexed directories ---
    def add_indexed_directory(self, directory):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO indexed_directories (path) VALUES (?)", (directory,))

    def get_indexed_directories(self):
        with self.conn:
            cursor = self.conn.execute("SELECT path FROM indexed_directories ORDER BY path")
            return [row[0] for row in cursor.fetchall()]

    def remove_indexed_directory(self, directory):
        with self.conn:
            # Remove the directory from the list
            self.conn.execute("DELETE FROM indexed_directories WHERE path = ?", (directory,))
            # Remove all image features that are within that directory path
            # Use LIKE to cover all files within the directory path
            self.conn.execute("DELETE FROM features WHERE path LIKE ?", (directory + '%',))


    def get_all_features(self):
        try:
            with self.conn:
                cursor = self.conn.execute("SELECT path, keypoints, descriptors FROM features")
                # Using fetchall() to be able to count the total for the progress bar
                return cursor.fetchall()
        except pickle.UnpicklingError as e:
            print(f"Error deserializing data: {e}. The database might be corrupt.")
            return []

    def get_all_paths(self):
        with self.conn:
            cursor = self.conn.execute("SELECT path FROM features")
            return {row[0] for row in cursor.fetchall()}

    def remove_paths(self, paths_to_remove):
        with self.conn:
            self.conn.executemany("DELETE FROM features WHERE path = ?", [(path,) for path in paths_to_remove])

    def clear_all_data(self):
        with self.conn:
            self.conn.execute("DELETE FROM features")
            self.conn.execute("DELETE FROM indexed_directories") # Also clear the directories table

    def close(self):
        self.conn.close()

# --- WORKER FOR INDEXING IN A SEPARATE THREAD (REMAINS THE SAME) ---
class IndexingWorker(QObject):
    indexing_finished = Signal(int, int)
    indexing_progress = Signal(int)

    def __init__(self, directory, db_path, allowed_extensions, force_reindex=False, parent=None):
        super().__init__(parent)
        self.directory = directory
        self.db_path = db_path
        self.allowed_extensions = allowed_extensions
        self.orb = cv2.ORB_create(nfeatures=2000)
        self.force_reindex = force_reindex

    def run(self):
        db_manager = DatabaseManager(self.db_path)
        
        image_paths_on_disk = self._get_image_paths()
        paths_in_db = db_manager.get_all_paths()
        
        paths_to_remove = {p for p in paths_in_db if p.startswith(self.directory)} - set(image_paths_on_disk)

        if paths_to_remove:
            db_manager.remove_paths(paths_to_remove)

        total_images = len(image_paths_on_disk)
        if total_images == 0:
            self.indexing_finished.emit(0, len(paths_to_remove))
            db_manager.close()
            return
            
        indexed_count = 0
        for i, path in enumerate(image_paths_on_disk):
            try:
                if path in paths_in_db and not self.force_reindex:
                    self.indexing_progress.emit(int((i + 1) / total_images * 100))
                    continue

                image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
                if image is None: continue

                keypoints, descriptors = self.orb.detectAndCompute(image, None)

                if descriptors is not None and keypoints:
                    db_manager.add_or_update_features(path, keypoints, descriptors)
                    indexed_count += 1

                self.indexing_progress.emit(int((i + 1) / total_images * 100))
            except Exception as e:
                print(f"Error processing {path}: {e}")
        
        db_manager.add_indexed_directory(self.directory)
        db_manager.close()
        self.indexing_finished.emit(indexed_count, len(paths_to_remove))

    def _get_image_paths(self):
        image_paths = []
        for root, _, files in os.walk(self.directory):
            for file in files:
                if file.lower().endswith(tuple(self.allowed_extensions)):
                    image_paths.append(os.path.join(root, file))
        return image_paths

# --- NEW: WORKER FOR SEARCHING IN A SEPARATE THREAD ---
class SearchWorker(QObject):
    search_finished = Signal(list)
    search_progress = Signal(int)

    def __init__(self, query_kps, query_des, db_path, min_match_count, parent=None):
        super().__init__(parent)
        self.query_kps = query_kps
        self.query_des = query_des
        self.db_path = db_path
        self.min_match_count = min_match_count
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)

    def run(self):
        db_manager = DatabaseManager(self.db_path)
        all_features = db_manager.get_all_features()
        db_manager.close()

        total_features = len(all_features)
        if total_features == 0:
            self.search_finished.emit([])
            return

        results = []
        for i, (path, kps_blob, des_blob) in enumerate(all_features):
            db_kps = deserialize_keypoints(kps_blob)
            db_des = pickle.loads(des_blob) if des_blob else None

            if db_des is None or len(db_kps) < self.min_match_count:
                continue
            
            matches = self.bf.knnMatch(self.query_des, db_des, k=2)
            good_matches = [m for m, n in (match for match in matches if len(match) == 2) if m.distance < 0.75 * n.distance]

            if len(good_matches) > self.min_match_count:
                src_pts = np.float32([self.query_kps[m.queryIdx].pt for m in good_matches]).reshape(-1, 1, 2)
                dst_pts = np.float32([db_kps[m.trainIdx].pt for m in good_matches]).reshape(-1, 1, 2)

                M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
                if M is not None:
                    num_inliers = mask.ravel().sum()
                    if num_inliers > self.min_match_count:
                        score = num_inliers / len(good_matches)
                        results.append((score, path, num_inliers))
            
            # Emit progress
            progress = int((i + 1) / total_features * 100)
            self.search_progress.emit(progress)
            
        results.sort(key=lambda x: x[2], reverse=True)
        self.search_finished.emit(results[:30])


# --- NEW: DIALOG TO MANAGE INDEXES ---
class ManageIndexesDialog(QDialog):
    # Signal to be sent when the user wants to re-index
    reindex_requested = Signal(str)

    def __init__(self, db_manager, parent=None):
        super().__init__(parent)
        self.db_manager = db_manager
        self.setWindowTitle("Manage Indexed Directories")
        self.setMinimumSize(600, 400)

        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        self.populate_list()
        layout.addWidget(self.list_widget)

        button_layout = QHBoxLayout()
        reindex_button = QPushButton("Re-index Selected")
        reindex_button.clicked.connect(self.reindex_selected)
        remove_button = QPushButton("Remove Selected")
        remove_button.clicked.connect(self.remove_selected)
        
        button_layout.addWidget(reindex_button)
        button_layout.addWidget(remove_button)
        layout.addLayout(button_layout)
        
        close_button = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_button.rejected.connect(self.reject)
        layout.addWidget(close_button)

    def populate_list(self):
        self.list_widget.clear()
        directories = self.db_manager.get_indexed_directories()
        self.list_widget.addItems(directories)

    def reindex_selected(self):
        selected_item = self.list_widget.currentItem()
        if selected_item:
            self.reindex_requested.emit(selected_item.text())
            self.accept() # Close the dialog
        else:
            QMessageBox.warning(self, "No Selection", "Please select a directory to re-index.")

    def remove_selected(self):
        selected_item = self.list_widget.currentItem()
        if not selected_item:
            QMessageBox.warning(self, "No Selection", "Please select a directory to remove from the index.")
            return

        directory_to_remove = selected_item.text()
        reply = QMessageBox.question(self, "Confirm Deletion",
                                       f"Are you sure you want to remove all index entries from the directory:\n{directory_to_remove}?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                       QMessageBox.StandardButton.No)
        
        if reply == QMessageBox.StandardButton.Yes:
            self.db_manager.remove_indexed_directory(directory_to_remove)
            self.populate_list()
            QMessageBox.information(self, "Completed", f"The index for {os.path.basename(directory_to_remove)} has been removed.")


# --- MAIN CLASS FOR UI AND SEARCH LOGIC (UPDATED) ---
class ImageSearchApp(QWidget):
    MIN_MATCH_COUNT = 10 

    def __init__(self):
        super().__init__()
        self.allowed_extensions = ('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tiff')

        # --- [CHANGE] Define the database path in AppData ---
        # Define the base application data directory (cross-platform)
        app_data_dir = Path(os.getenv('LOCALAPPDATA', Path.home() / '.local/share')) / 'MacanImageViewer'
        # Create the directory if it doesn't exist; it won't error if it already does
        app_data_dir.mkdir(parents=True, exist_ok=True)
        # Define the full path to the database file
        self.db_path = str(app_data_dir / "image_index.db")
        # --- [END OF CHANGE] ---

        self.db_manager = DatabaseManager(self.db_path)
        self.orb = cv2.ORB_create(nfeatures=2000)        
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle('Macan Image Finder (Search by Image)')
        self.setGeometry(100, 100, 1200, 800)        
        
        icon_path = "icon.ico"
        if hasattr(sys, "_MEIPASS"):
            icon_path = os.path.join(sys._MEIPASS, icon_path)
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
        main_layout = QHBoxLayout()
        left_panel = QVBoxLayout()
        self.query_label = QLabel('Select an Image to Search')
        self.query_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.query_label.setStyleSheet("border: 2px dashed #888; padding: 10px; background-color: #f0f0f0;")
        self.query_label.setFixedSize(300, 300)
        
        self.load_button = QPushButton('Select Query Image')
        self.load_button.clicked.connect(self.select_query_image)
        
        self.add_index_button = QPushButton('Add Directory to Index')
        self.add_index_button.clicked.connect(self.start_indexing)
        
        # --- NEW: Buttons to manage and clear indexes ---
        self.manage_indexes_button = QPushButton("Manage Indexes")
        self.manage_indexes_button.clicked.connect(self.open_manage_indexes_dialog)
        
        self.clear_all_button = QPushButton("Clear All Indexes")
        self.clear_all_button.setStyleSheet("background-color: #ffdddd;")
        self.clear_all_button.clicked.connect(self.clear_all_indexes)

        # --- CHANGED: Separate progress bars for indexing and searching ---
        self.indexing_progress_bar = QProgressBar()
        self.indexing_progress_bar.setVisible(False)
        self.search_progress_bar = QProgressBar()
        self.search_progress_bar.setVisible(False)
        self.search_progress_bar.setTextVisible(True)
        self.search_progress_bar.setFormat("Searching... %p%")


        left_panel.addWidget(self.query_label)
        left_panel.addWidget(self.load_button)
        left_panel.addSpacing(20)
        left_panel.addWidget(self.add_index_button)
        left_panel.addWidget(self.manage_indexes_button)
        left_panel.addWidget(self.clear_all_button)
        left_panel.addWidget(QLabel("Indexing Progress:"))
        left_panel.addWidget(self.indexing_progress_bar)
        left_panel.addWidget(QLabel("Search Progress:"))
        left_panel.addWidget(self.search_progress_bar)
        left_panel.addStretch()

        right_panel = QVBoxLayout()
        results_label = QLabel('Search Results (Closest Matches)')
        results_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.results_list = QListWidget()
        self.results_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.results_list.setIconSize(QSize(120, 120))
        self.results_list.setSpacing(15)
        self.results_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.results_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results_list.customContextMenuRequested.connect(self.show_context_menu)
        # --- NEW: Connect item selection to the status bar ---
        self.results_list.currentItemChanged.connect(self.update_status_bar)
        
        right_panel.addWidget(results_label)
        right_panel.addWidget(self.results_list)
        
        # --- NEW: Status bar ---
        self.status_bar = QStatusBar()
        self.status_bar.showMessage("Ready")
        
        # Combine the main layouts
        top_layout = QHBoxLayout()
        top_layout.addLayout(left_panel, 1)
        top_layout.addLayout(right_panel, 3)
        
        main_v_layout = QVBoxLayout(self)
        main_v_layout.addLayout(top_layout)
        main_v_layout.addWidget(self.status_bar)


    def select_query_image(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Image", "", f"Image Files (*{' *'.join(self.allowed_extensions)})")
        if file_path:
            pixmap = QPixmap(file_path)
            self.query_label.setPixmap(pixmap.scaled(self.query_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self.run_search_thread(file_path) # CHANGED: Call the new thread function

    def start_indexing(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Directory to Index")
        if directory:
            self.run_indexing_thread(directory, force_reindex=False)
            
    # --- NEW: Function for the management dialog ---
    def open_manage_indexes_dialog(self):
        dialog = ManageIndexesDialog(self.db_manager, self)
        # Connect the signal from the dialog to the re-indexing function in the main window
        dialog.reindex_requested.connect(self.reindex_directory)
        dialog.exec()
        
    def reindex_directory(self, directory):
        reply = QMessageBox.question(self, "Confirm Re-index",
                                       f"Are you sure you want to re-index the directory:\n{directory}?\nThis will rescan all images to update their features.",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                       QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.run_indexing_thread(directory, force_reindex=True)

    def clear_all_indexes(self):
        reply = QMessageBox.warning(self, "Confirm Total Deletion",
                                       "WARNING: This action will permanently delete the index database file (`image_index.db`) from the disk.\n\nAre you sure you want to continue?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                       QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            try:
                # Get the file path before closing the connection
                db_file_path = self.db_manager.db_path
                
                # 1. Close the connection to the database so the file is not locked
                self.db_manager.close()
                
                # 2. Delete the physical file from storage if it exists
                if os.path.exists(db_file_path):
                    os.remove(db_file_path)
                
                # 3. Recreate the DatabaseManager instance so the application
                #    creates a new, empty DB file ready for use again.
                self.db_manager = DatabaseManager(self.db_path)
                
                # Update UI
                self.results_list.clear()
                self.status_bar.showMessage("Index file has been deleted and recreated.")
                QMessageBox.information(self, "Completed", "The index database file has been successfully deleted.")
            except Exception as e:
                QMessageBox.critical(self, "Deletion Failed", f"Failed to delete the database file: {e}\n\nThe application may need to be restarted.")
                # If it fails, try to recreate the connection
                self.db_manager = DatabaseManager(self.db_path)


    def run_indexing_thread(self, directory, force_reindex=False):
        self.status_bar.showMessage(f'Indexing: {os.path.basename(directory)}...')
        self.set_controls_enabled(False)
        self.indexing_progress_bar.setVisible(True)
        self.indexing_progress_bar.setValue(0)

        self.indexing_thread = QThread()
        self.worker = IndexingWorker(directory, self.db_path, self.allowed_extensions, force_reindex)
        self.worker.moveToThread(self.indexing_thread)

        self.indexing_thread.started.connect(self.worker.run)
        self.worker.indexing_finished.connect(self.on_indexing_finished)
        self.worker.indexing_progress.connect(self.indexing_progress_bar.setValue)
        
        self.worker.indexing_finished.connect(self.indexing_thread.quit)
        self.indexing_thread.finished.connect(self.indexing_thread.deleteLater)
        self.worker.indexing_finished.connect(self.worker.deleteLater)

        self.indexing_thread.start()

    def on_indexing_finished(self, indexed_count, deleted_count):
        self.set_controls_enabled(True)
        self.indexing_progress_bar.setVisible(False)
        self.status_bar.showMessage(f"Indexing finished. {indexed_count} images processed, {deleted_count} entries removed.")
        QMessageBox.information(self, "Indexing Complete", f"{indexed_count} images were processed/updated.\n{deleted_count} old entries were removed.")

    def set_controls_enabled(self, enabled):
        self.load_button.setEnabled(enabled)
        self.add_index_button.setEnabled(enabled)
        self.manage_indexes_button.setEnabled(enabled)
        self.clear_all_button.setEnabled(enabled)


    # --- CHANGED: The search process now runs in a separate thread ---
    def run_search_thread(self, query_path):
        try:
            query_image_bgr = cv2.imread(query_path)
            if query_image_bgr is None: raise ValueError("Failed to load the query image.")

            query_image_gray = cv2.cvtColor(query_image_bgr, cv2.COLOR_BGR2GRAY)
            query_kps, query_des = self.orb.detectAndCompute(query_image_gray, None)

            if query_des is None or len(query_kps) < self.MIN_MATCH_COUNT:
                QMessageBox.warning(self, "Error", "Not enough features were found in the query image.")
                return
            
            if not self.db_manager.get_indexed_directories():
                QMessageBox.warning(self, "Warning", "The index is empty. Please add a directory and start the indexing process.")
                return

            self.set_controls_enabled(False)
            self.search_progress_bar.setVisible(True)
            self.search_progress_bar.setValue(0)
            self.results_list.clear() # Clear old results
            self.status_bar.showMessage("Searching for similar images...")

            self.search_thread = QThread()
            self.search_worker = SearchWorker(query_kps, query_des, self.db_path, self.MIN_MATCH_COUNT)
            self.search_worker.moveToThread(self.search_thread)
            
            self.search_thread.started.connect(self.search_worker.run)
            self.search_worker.search_progress.connect(self.search_progress_bar.setValue)
            self.search_worker.search_finished.connect(self.on_search_finished)

            self.search_worker.search_finished.connect(self.search_thread.quit)
            self.search_thread.finished.connect(self.search_thread.deleteLater)
            self.search_worker.search_finished.connect(self.search_worker.deleteLater)
            
            self.search_thread.start()

        except Exception as e:
            QMessageBox.critical(self, "Search Error", f"An error occurred: {e}")
            self.set_controls_enabled(True)
            self.search_progress_bar.setVisible(False)

    def on_search_finished(self, results):
        self.set_controls_enabled(True)
        self.search_progress_bar.setVisible(False)
        self.display_results(results)
        self.status_bar.showMessage(f"Search complete. Found {len(results)} relevant results.")
        if not results:
            QMessageBox.information(self, "Search Results", "No sufficiently similar images were found in the index.")


    def display_results(self, sorted_results):
        self.results_list.clear()
        for score, path, num_inliers in sorted_results:
            if not os.path.exists(path): continue
            item = QListWidgetItem()
            pixmap = QPixmap(path)
            item.setIcon(QIcon(pixmap.scaled(QSize(120, 120), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))
            item.setText(f"{os.path.basename(path)}\nInliers: {int(num_inliers)}\nScore: {score:.2%}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.results_list.addItem(item)
    
    # --- NEW: Function to update the status bar ---
    def update_status_bar(self, current_item, previous_item):
        if current_item:
            path = current_item.data(Qt.ItemDataRole.UserRole)
            self.status_bar.showMessage(path)

    # --- Remaining functions (show_context_menu, open_file_location, closeEvent) remain the same ---
    def show_context_menu(self, position):
        item = self.results_list.itemAt(position)
        if item and item.data(Qt.ItemDataRole.UserRole):
            menu = QMenu()
            open_action = QAction("Open File Location", self)
            open_action.triggered.connect(lambda: self.open_file_location(item.data(Qt.ItemDataRole.UserRole)))
            menu.addAction(open_action)
            menu.exec(self.results_list.mapToGlobal(position))

    def open_file_location(self, path):
        if not os.path.exists(path):
            QMessageBox.warning(self, "File Not Found", f"The file does not exist at:\n{path}")
            return
        try:
            if platform.system() == "Windows":
                subprocess.run(['explorer', '/select,', os.path.normpath(path)])
            elif platform.system() == "Darwin":
                subprocess.run(['open', '-R', path])
            else:
                subprocess.run(['xdg-open', os.path.dirname(path)])
        except Exception as e:
            QMessageBox.critical(self, "Failed to Open Directory", f"An error occurred: {e}")
    
    def closeEvent(self, event):
        self.db_manager.close()
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    viewer = ImageSearchApp()
    viewer.show()
    sys.exit(app.exec())