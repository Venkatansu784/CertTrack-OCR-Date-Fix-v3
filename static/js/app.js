(() => {
  // User-controlled light/dark theme, remembered only in this browser.
  const themeButton = document.getElementById('theme-toggle');
  const storedTheme = (() => { try { return localStorage.getItem('certtrack-theme'); } catch (_) { return null; } })();
  const applyTheme = theme => {
    document.documentElement.dataset.theme = theme;
    if (themeButton) {
      const icon = themeButton.querySelector('.theme-toggle-icon');
      const label = themeButton.querySelector('.theme-toggle-label');
      if (icon) icon.textContent = theme === 'dark' ? '☀' : '☾';
      if (label) label.textContent = theme === 'dark' ? 'Light mode' : 'Dark mode';
      themeButton.setAttribute('aria-pressed', theme === 'dark' ? 'true' : 'false');
    }
  };
  applyTheme(storedTheme === 'dark' ? 'dark' : 'light');
  themeButton?.addEventListener('click', () => {
    const nextTheme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(nextTheme);
    try { localStorage.setItem('certtrack-theme', nextTheme); } catch (_) { /* Storage may be disabled. */ }
  });

  const menu = document.getElementById('mobile-menu');
  const sidebar = document.getElementById('sidebar');
  if (menu && sidebar) menu.addEventListener('click', () => sidebar.classList.toggle('open'));

  document.querySelectorAll('.flash-close').forEach(button => {
    button.addEventListener('click', () => button.closest('.flash')?.remove());
  });

  document.querySelectorAll('[data-fill-user]').forEach(button => {
    button.addEventListener('click', () => {
      const username = document.querySelector('input[name="username"]');
      const password = document.querySelector('input[name="password"]');
      if (username && password) {
        username.value = button.dataset.fillUser || '';
        password.value = button.dataset.fillPassword || '';
        password.focus();
      }
    });
  });

  document.querySelectorAll('form[data-confirm]').forEach(form => {
    form.addEventListener('submit', event => {
      if (!window.confirm(form.dataset.confirm || 'Continue?')) event.preventDefault();
    });
  });

  const fileInput = document.getElementById('certificate-file');
  const dropZone = document.getElementById('drop-zone');
  const fileLabel = document.getElementById('file-label');
  const fileSubtitle = document.getElementById('file-subtitle');
  const scanButton = document.getElementById('scan-button');
  if (fileInput && dropZone) {
    const showFile = file => {
      if (!file) return;
      fileLabel.textContent = file.name;
      fileSubtitle.textContent = `${(file.size / (1024 * 1024)).toFixed(2)} MB · Ready to scan`;
      dropZone.classList.add('has-file');
    };
    fileInput.addEventListener('change', () => showFile(fileInput.files?.[0]));
    ['dragenter', 'dragover'].forEach(name => dropZone.addEventListener(name, event => {
      event.preventDefault(); dropZone.classList.add('drag-over');
    }));
    ['dragleave', 'drop'].forEach(name => dropZone.addEventListener(name, event => {
      event.preventDefault(); dropZone.classList.remove('drag-over');
    }));
    dropZone.addEventListener('drop', event => {
      const files = event.dataTransfer?.files;
      if (files && files.length) {
        try { fileInput.files = files; } catch (_) { /* Browser may restrict assignment; the user can browse instead. */ }
        showFile(files[0]);
      }
    });
    const uploadForm = document.getElementById('upload-form');
    if (uploadForm && scanButton) uploadForm.addEventListener('submit', () => {
      if (!fileInput.files || !fileInput.files.length) return;
      scanButton.disabled = true;
      scanButton.innerHTML = '<span class="spinner" aria-hidden="true"></span> Reading certificate…';
    });
  }

  const issue = document.querySelector('input[name="issue_date"]');
  const expiry = document.querySelector('input[name="expiry_date"]');
  const previewText = document.getElementById('date-preview-text');
  const previewStatus = document.getElementById('date-preview-status');
  if (issue && expiry && previewText && previewStatus) {
    const updatePreview = () => {
      const a = issue.value ? new Date(`${issue.value}T00:00:00`) : null;
      const b = expiry.value ? new Date(`${expiry.value}T00:00:00`) : null;
      previewStatus.className = 'status status-pending';
      if (!a || !b || Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) {
        previewText.textContent = 'Select the issue and expiry dates to preview the compliance status.';
        previewStatus.textContent = 'Not calculated';
        return;
      }
      if (b <= a) {
        previewText.textContent = 'The expiry date must be after the issue date.';
        previewStatus.textContent = 'Check dates';
        previewStatus.className = 'status status-expired';
        return;
      }
      const today = new Date(); today.setHours(0, 0, 0, 0);
      const days = Math.round((b - today) / 86400000);
      if (days < 0) {
        previewText.textContent = `This certificate expired ${Math.abs(days)} day${Math.abs(days) === 1 ? '' : 's'} ago.`;
        previewStatus.textContent = 'Expired'; previewStatus.className = 'status status-expired';
      } else if (days <= 30) {
        previewText.textContent = days === 0 ? 'This certificate expires today.' : `This certificate expires in ${days} day${days === 1 ? '' : 's'}.`;
        previewStatus.textContent = 'Expiring Soon'; previewStatus.className = 'status status-expiring';
      } else {
        previewText.textContent = `${days} days until this certificate expires.`;
        previewStatus.textContent = 'Valid'; previewStatus.className = 'status status-valid';
      }
    };
    issue.addEventListener('change', updatePreview);
    expiry.addEventListener('change', updatePreview);
    updatePreview();
  }
})();
