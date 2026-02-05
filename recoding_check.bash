python3 - <<'PY'
import cv2, numpy as np

p = "/Users/afhambashir/ASICAP/CapObj/2026-01-30Z/2026-01-30-2338_2-CapObj_mjpeg.avi"
cap = cv2.VideoCapture(p)
print("opened:", cap.isOpened())
print("fps:", cap.get(cv2.CAP_PROP_FPS))
print("w,h:", int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))

# Grab a few frames spread out
idxs = [0, 2000, 5000, 10000, 20000]
for idx in idxs:
    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
    ok, frame = cap.read()
    print("\nframe", idx, "ok:", ok)
    if not ok:
        continue

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    print("gray dtype:", gray.dtype, "min/max/mean:", int(gray.min()), int(gray.max()), float(gray.mean()))

    # Save one image so you can visually confirm the blob is in the decoded pixels
    if idx == idxs[1]:
        cv2.imwrite("debug_frame.png", gray)
        print("wrote debug_frame.png")

cap.release()
PY
