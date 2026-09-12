import cv2
import numpy as np

img_path = r"C:/Users/Zabaoit/.gemini/antigravity/brain/d418fa3e-d05a-4748-9f4c-d17d1937acb0/.user_uploaded/media_1788502029759.png"
img = cv2.imread(img_path)
detector = cv2.QRCodeDetector()
data, bbox, _ = detector.detectAndDecode(img)
print("DECODED QR DATA:")
print(data)
