/**
 * FiCol Evidence Camera & Client-Side Compression Pipeline
 * Captures photos directly from device camera and compresses them via Canvas
 * to ensure lightweight storage in IndexedDB and fast uploads over 2G/3G EDGE networks.
 */
(() => {
  'use strict';

  function compressImage(file, maxDimension = 1280, quality = 0.75) {
    return new Promise((resolve, reject) => {
      if (!file || !file.type.startsWith('image/')) {
        return reject(new Error('Please select or capture a valid image file.'));
      }

      const reader = new FileReader();
      reader.onerror = () => reject(new Error('Failed to read image file.'));
      reader.onload = (e) => {
        const img = new Image();
        img.onerror = () => reject(new Error('Failed to decode captured image.'));
        img.onload = () => {
          let width = img.width;
          let height = img.height;

          // Scale down proportionally
          if (width > maxDimension || height > maxDimension) {
            if (width > height) {
              height = Math.round((height * maxDimension) / width);
              width = maxDimension;
            } else {
              width = Math.round((width * maxDimension) / height);
              height = maxDimension;
            }
          }

          const canvas = document.createElement('canvas');
          canvas.width = width;
          canvas.height = height;

          const ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0, width, height);

          canvas.toBlob(
            (blob) => {
              if (!blob) {
                return reject(new Error('Failed to compress image canvas.'));
              }
              const cleanBase = (file.name || 'evidence.jpg').replace(/\.[^/.]+$/, '');
              const compressedFileName = `${cleanBase}_compressed.jpg`;

              resolve({
                blob: blob,
                dataUrl: canvas.toDataURL('image/jpeg', quality),
                fileName: compressedFileName,
                mimeType: 'image/jpeg',
                sizeBytes: blob.size,
                originalSizeBytes: file.size,
                width: width,
                height: height,
              });
            },
            'image/jpeg',
            quality
          );
        };
        img.src = e.target.result;
      };
      reader.readAsDataURL(file);
    });
  }

  function initEvidenceCamera(config) {
    const inputEl = document.getElementById(config.inputId || 'evidence-camera-input');
    const triggerBtn = document.getElementById(config.triggerBtnId || 'evidence-camera-btn');
    const previewContainer = document.getElementById(config.previewContainerId || 'evidence-photo-preview');
    const hintEl = document.getElementById(config.hintId || 'evidence-photo-hint');

    let currentPhoto = null;

    if (!inputEl) return null;

    if (triggerBtn) {
      triggerBtn.addEventListener('click', (e) => {
        e.preventDefault();
        inputEl.click();
      });
    }

    inputEl.addEventListener('change', async (e) => {
      const file = e.target.files?.[0];
      if (!file) return;

      if (hintEl) {
        hintEl.textContent = 'Compressing photo for offline storage...';
        hintEl.style.color = '#4f46e5';
      }

      try {
        const compressed = await compressImage(file, 1280, 0.75);
        currentPhoto = compressed;

        const origKb = Math.round(compressed.originalSizeBytes / 1024);
        const compKb = Math.round(compressed.sizeBytes / 1024);

        if (previewContainer) {
          previewContainer.innerHTML = `
            <div style="position: relative; display: inline-block; margin-top: 8px; border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px; background: #f8fafc;">
              <img src="${compressed.dataUrl}" alt="Evidence preview" style="max-width: 140px; max-height: 140px; border-radius: 4px; display: block; object-fit: cover;">
              <div style="font-size: 11px; color: #475569; margin-top: 4px; font-weight: 500;">
                📸 ${compKb} KB <span style="color: #16a34a; font-size: 10px;">(reduced from ${origKb} KB)</span>
              </div>
              <button type="button" id="evidence-remove-btn" style="margin-top: 4px; padding: 2px 8px; font-size: 11px; background: #fee2e2; color: #991b1b; border: 1px solid #fecaca; border-radius: 4px; cursor: pointer; width: 100%;">
                ✕ Remove Photo
              </button>
            </div>
          `;

          const removeBtn = document.getElementById('evidence-remove-btn');
          if (removeBtn) {
            removeBtn.addEventListener('click', () => {
              currentPhoto = null;
              inputEl.value = '';
              previewContainer.innerHTML = '';
              if (hintEl) {
                hintEl.textContent = 'Take a clear photo of the house, entrance, or receipt.';
                hintEl.style.color = '';
              }
            });
          }
        }

        if (hintEl) {
          hintEl.textContent = 'Photo attached and optimized for offline save.';
          hintEl.style.color = '#16a34a';
        }
      } catch (err) {
        console.error('Camera capture error:', err);
        if (hintEl) {
          hintEl.textContent = 'Failed to process photo: ' + err.message;
          hintEl.style.color = '#dc2626';
        }
      }
    });

    return {
      getPhoto() {
        return currentPhoto;
      },
      clear() {
        currentPhoto = null;
        if (inputEl) inputEl.value = '';
        if (previewContainer) previewContainer.innerHTML = '';
        if (hintEl) {
          hintEl.textContent = 'Take a clear photo of the house, entrance, or receipt.';
          hintEl.style.color = '';
        }
      },
      setExisting(dataUrl, fileName) {
        if (!dataUrl) return;
        currentPhoto = { dataUrl, fileName };
        if (previewContainer) {
          previewContainer.innerHTML = `
            <div style="display: inline-block; margin-top: 8px; border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px; background: #f8fafc;">
              <img src="${dataUrl}" alt="Evidence preview" style="max-width: 140px; max-height: 140px; border-radius: 4px; display: block; object-fit: cover;">
              <div style="font-size: 11px; color: #16a34a; margin-top: 4px;">📸 Saved Offline Photo</div>
            </div>
          `;
        }
      },
    };
  }

  window.FiColCamera = {
    compressImage,
    initEvidenceCamera,
  };
})();
