import './custom-select.css';

const registry = new WeakMap();

export function enhanceSelect(selectEl, options = {}) {
  if (!selectEl) return null;
  if (registry.has(selectEl)) {
    return registry.get(selectEl);
  }
  const instance = new CustomSelect(selectEl, options);
  registry.set(selectEl, instance);
  return instance;
}

function normalizeSearch(value) {
  return String(value || '').trim().toLowerCase();
}

class CustomSelect {
  constructor(selectEl, { placeholder = 'Выберите…', searchable = true, searchPlaceholder = 'Поиск…' } = {}) {
    this.select = selectEl;
    this.multiple = selectEl.multiple;
    this.placeholder = placeholder;
    this.searchable = searchable;
    this.searchPlaceholder = searchPlaceholder;
    this.open = false;
    this.searchQuery = '';

    selectEl.classList.add('custom-select__native');

    this.root = document.createElement('div');
    this.root.className = 'custom-select';
    if (this.multiple) {
      this.root.classList.add('custom-select--multiple');
    }

    selectEl.parentNode.insertBefore(this.root, selectEl);
    this.root.appendChild(selectEl);

    this.trigger = document.createElement('button');
    this.trigger.type = 'button';
    this.trigger.className = 'custom-select__trigger';
    this.trigger.setAttribute('aria-haspopup', 'listbox');
    this.trigger.setAttribute('aria-expanded', 'false');
    this.trigger.innerHTML =
      '<span class="custom-select__value"></span><span class="custom-select__chevron" aria-hidden="true"></span>';
    this.valueEl = this.trigger.querySelector('.custom-select__value');

    this.menu = document.createElement('div');
    this.menu.className = 'custom-select__menu';
    this.menu.hidden = true;

    this.searchWrap = document.createElement('div');
    this.searchWrap.className = 'custom-select__search-wrap';
    this.searchInput = document.createElement('input');
    this.searchInput.type = 'search';
    this.searchInput.className = 'custom-select__search';
    this.searchInput.placeholder = this.searchPlaceholder;
    this.searchInput.autocomplete = 'off';
    this.searchInput.setAttribute('aria-label', this.searchPlaceholder);
    this.searchWrap.appendChild(this.searchInput);

    this.list = document.createElement('ul');
    this.list.className = 'custom-select__list';
    this.list.setAttribute('role', 'listbox');

    this.emptyState = document.createElement('div');
    this.emptyState.className = 'custom-select__empty';
    this.emptyState.textContent = 'Ничего не найдено';
    this.emptyState.hidden = true;

    this.menu.appendChild(this.searchWrap);
    this.menu.appendChild(this.list);
    this.menu.appendChild(this.emptyState);

    this.root.appendChild(this.trigger);
    this.root.appendChild(this.menu);

    this.onTriggerClick = (event) => {
      event.preventDefault();
      event.stopPropagation();
      this.toggle();
    };

    this.onOutsideClick = (event) => {
      if (!this.root.contains(event.target)) {
        this.close();
      }
    };

    this.onKeydown = (event) => {
      if (event.key === 'Escape') {
        this.close();
      }
    };

    this.onSearchInput = (event) => {
      event.stopPropagation();
      this.searchQuery = this.searchInput.value;
      this.applySearch();
    };

    this.onMenuClick = (event) => {
      event.stopPropagation();
    };

    this.trigger.addEventListener('click', this.onTriggerClick);
    this.searchInput.addEventListener('input', this.onSearchInput);
    this.searchInput.addEventListener('click', (event) => event.stopPropagation());
    this.menu.addEventListener('click', this.onMenuClick);
    this.refresh();
  }

  toggle() {
    if (this.open) {
      this.close();
    } else {
      this.openMenu();
    }
  }

  openMenu() {
    document.querySelectorAll('.custom-select[data-open="true"]').forEach((node) => {
      const select = node.querySelector('.custom-select__native');
      const instance = registry.get(select);
      if (instance && instance !== this) {
        instance.close();
      }
    });

    this.open = true;
    this.root.dataset.open = 'true';
    this.menu.hidden = false;
    this.trigger.setAttribute('aria-expanded', 'true');
    this.updateSearchVisibility();
    this.resetSearch();
    document.addEventListener('click', this.onOutsideClick);
    document.addEventListener('keydown', this.onKeydown);

    if (this.searchable && this.select.options.length > 0) {
      requestAnimationFrame(() => this.searchInput.focus());
    }
  }

  close() {
    this.open = false;
    this.root.dataset.open = 'false';
    this.menu.hidden = true;
    this.trigger.setAttribute('aria-expanded', 'false');
    this.resetSearch();
    document.removeEventListener('click', this.onOutsideClick);
    document.removeEventListener('keydown', this.onKeydown);
  }

  resetSearch() {
    this.searchQuery = '';
    this.searchInput.value = '';
    this.applySearch();
  }

  updateSearchVisibility() {
    const showSearch = this.searchable && this.select.options.length > 0;
    this.searchWrap.hidden = !showSearch;
    this.root.classList.toggle('custom-select--searchable', showSearch);
  }

  applySearch() {
    const query = normalizeSearch(this.searchQuery);
    let visibleCount = 0;

    Array.from(this.list.children).forEach((item) => {
      const haystack = item.dataset.searchText || '';
      const matches = !query || haystack.includes(query);
      item.hidden = !matches;
      if (matches) {
        visibleCount += 1;
      }
    });

    this.emptyState.hidden = visibleCount > 0;
    this.list.hidden = visibleCount === 0;
  }

  refresh() {
    this.list.innerHTML = '';

    Array.from(this.select.options).forEach((option) => {
      const item = document.createElement('li');
      item.className = 'custom-select__option';
      item.dataset.value = option.value;
      item.dataset.searchText = normalizeSearch(option.textContent);
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', option.selected ? 'true' : 'false');

      if (option.selected) {
        item.classList.add('is-selected');
      }
      if (option.disabled) {
        item.classList.add('is-disabled');
      }

      if (this.multiple) {
        item.innerHTML = `
          <span class="custom-select__check" aria-hidden="true"></span>
          <span class="custom-select__label">${option.textContent}</span>
        `;
      } else {
        item.innerHTML = `<span class="custom-select__label">${option.textContent}</span>`;
      }

      item.addEventListener('click', (event) => {
        event.stopPropagation();
        if (option.disabled) return;
        this.choose(option.value);
      });

      this.list.appendChild(item);
    });

    this.updateSearchVisibility();
    this.applySearch();
    this.syncFromNative();
  }

  choose(value) {
    if (this.multiple) {
      const option = Array.from(this.select.options).find((entry) => entry.value === value);
      if (!option) return;
      option.selected = !option.selected;
      this.syncFromNative();
      this.select.dispatchEvent(new Event('change', { bubbles: true }));
      return;
    }

    this.select.value = value;
    this.close();
    this.syncFromNative();
    this.select.dispatchEvent(new Event('change', { bubbles: true }));
  }

  syncFromNative() {
    Array.from(this.list.children).forEach((item) => {
      const option = Array.from(this.select.options).find((entry) => entry.value === item.dataset.value);
      const selected = Boolean(option?.selected);
      item.classList.toggle('is-selected', selected);
      item.setAttribute('aria-selected', selected ? 'true' : 'false');
    });
    this.syncDisplay();
  }

  syncDisplay() {
    if (this.multiple) {
      const selected = Array.from(this.select.selectedOptions);
      if (!selected.length) {
        this.valueEl.textContent = this.placeholder;
        this.root.classList.remove('has-value');
      } else if (selected.length <= 2) {
        this.valueEl.textContent = selected.map((option) => option.textContent).join(', ');
        this.root.classList.add('has-value');
      } else {
        this.valueEl.textContent = `Выбрано: ${selected.length}`;
        this.root.classList.add('has-value');
      }
      return;
    }

    const option = this.select.options[this.select.selectedIndex];
    const hasValue = Boolean(option?.value);
    this.valueEl.textContent = option?.textContent || this.placeholder;
    this.root.classList.toggle('has-value', hasValue || this.select.selectedIndex > 0);
  }
}
