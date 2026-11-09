import os
import shutil
import ctypes
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

# --- Логика очистки -------------------------------------------------------

SHERB_FLAGS = 0x1 | 0x2 | 0x4  # без подтверждений, прогресса и звука


def empty_recycle_bin():
    ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, SHERB_FLAGS)


def _size_of_folder(path: Path) -> int:
    """Суммарный размер файлов в папке (до удаления)."""
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def clear_folder(path: str) -> int:
    """Удаляет содержимое папки, возвращает сколько байт освободилось.

    Размер считается ДО удаления. Если что-то не удалилось из-за
    блокировки (например, запущен браузер), это вернёт завышенную
    оценку — это честно оговорено в отчёте.
    """
    folder = Path(path)
    if not folder.exists():
        return 0

    freed = 0
    for item in list(folder.iterdir()):
        try:
            if item.is_dir():
                freed += _size_of_folder(item)
                shutil.rmtree(item, ignore_errors=True)
            else:
                freed += item.stat().st_size
                item.unlink()
        except (PermissionError, OSError):
            pass  # файл занят — пропускаем
    return freed


def local_app_cache(name: str) -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), name)


def group_name(key: str) -> str:
    """Человекочитаемое имя браузера для отчёта."""
    return {
        "yandex": "Яндекс.Браузер",
        "chrome": "Google Chrome",
        "edge": "Microsoft Edge",
    }.get(key, key)


def clear_browser_history(profile_dir: str) -> int:
    """Удаляет файлы истории посещений (History*) в профиле браузера.

    Возвращает количество удалённых файлов. Браузер должен быть закрыт,
    иначе SQLite-файлы заняты и будут пропущены.
    """
    removed = 0
    path = Path(local_app_cache(profile_dir))
    if not path.exists():
        return removed

    # История хранится в файлах History и History-journal в корне профиля
    for name in ("History", "History-journal"):
        f = path / name
        try:
            if f.exists():
                f.unlink()
                removed += 1
        except PermissionError:
            pass  # файл занят браузером
    return removed


# --- Сворачиваемая группа с под-галочками ---------------------------------

BG = "#f4f5f7"


class CollapsibleCheckGroup(tk.Frame):
    """Группа чекбоксов: заголовок с галкой-«мастером» и стрелкой,
    раскрывающей дополнительные галочки (кэш / история).
    """

    def __init__(self, master, title, items):
        super().__init__(master, bg=BG)
        self._expanded = True
        self._syncing = False
        self._item_vars = {}

        header = tk.Frame(self, bg=BG)
        header.pack(fill="x")

        self._chevron = tk.Label(header, text="▼", cursor="hand2",
                                 bg=BG, fg="#777",
                                 font=("Segoe UI", 9))
        self._chevron.pack(side="left", padx=(0, 6))
        self._chevron.bind("<Button-1>", lambda e: self.toggle())

        self.master_var = tk.BooleanVar(value=True)
        cb = tk.Checkbutton(header, text=title, variable=self.master_var,
                            command=self._apply_master,
                            font=("Segoe UI", 11, "bold"), bg=BG,
                            anchor="w", activebackground=BG)
        cb.pack(side="left")

        self._body = tk.Frame(self, bg=BG)
        for key, label in items:
            v = tk.BooleanVar(value=True)
            self._item_vars[key] = v
            v.trace_add("write", lambda *a, k=key: self._sync_master_from_items(k))
            tk.Checkbutton(self._body, text=label, variable=v,
                           font=("Segoe UI", 10), bg=BG,
                           anchor="w", activebackground=BG
                           ).pack(anchor="w", padx=(40, 20))

        self._render()
        self._apply_master()

    def opt(self, key):
        return self._item_vars[key]

    def toggle(self):
        self._expanded = not self._expanded
        self._render()

    def _render(self):
        self._chevron.config(text="▾" if self._expanded else "▸")
        if self._expanded:
            self._body.pack(fill="x")
        else:
            self._body.pack_forget()

    def _apply_master(self):
        val = self.master_var.get()
        self._syncing = True
        try:
            for v in self._item_vars.values():
                v.set(val)
        finally:
            self._syncing = False

    def _sync_master_from_items(self, key):
        if self._syncing:
            return
        any_checked = any(v.get() for v in self._item_vars.values())
        self.master_var.set(any_checked)


# --- Интерфейс ------------------------------------------------------------

class CleanerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Очистка ПК")
        self.geometry("440x640")
        self.minsize(420, 560)
        self.resizable(True, True)
        self.configure(bg="#f4f5f7")
        self._result_queue = queue.Queue()
        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 20, "pady": 4}

        title = tk.Label(self, text="Очистка компьютера",
                         font=("Segoe UI", 16, "bold"), bg="#f4f5f7", fg="#222")
        title.pack(pady=(18, 2))

        subtitle = tk.Label(self, text="Выберите, что нужно очистить:",
                            font=("Segoe UI", 10), bg="#f4f5f7", fg="#666")
        subtitle.pack(pady=(0, 10))

        self.vars = {}

        def add_check(text, key):
            var = tk.BooleanVar(value=True)
            self.vars[key] = var
            cb = tk.Checkbutton(self, text=text, variable=var,
                                font=("Segoe UI", 11), bg="#f4f5f7",
                                anchor="w", activebackground="#f4f5f7")
            cb.pack(fill="x", **pad)

        add_check("Корзина", "bin")
        add_check("Временные файлы Windows (Temp)", "temp")

        self.browser_groups = {}
        for gkey, gtitle, items in [
            ("yandex", "Файлы Яндекс.Браузера",
             [("yandex_cache", "Кэш браузера"),
              ("yandex_history", "История браузера")]),
            ("chrome", "Файлы Google Chrome",
             [("chrome_cache", "Кэш браузера"),
              ("chrome_history", "История браузера")]),
            ("edge", "Файлы Microsoft Edge",
             [("edge_cache", "Кэш браузера"),
              ("edge_history", "История браузера")]),
        ]:
            grp = CollapsibleCheckGroup(self, gtitle, items)
            grp.pack(fill="x", **pad)
            self.browser_groups[gkey] = grp

        self.progress = ttk.Progressbar(self, mode="indeterminate", length=360)
        self.progress.pack(pady=(14, 6))

        self.status = tk.Label(self, text="Готово к очистке",
                               font=("Segoe UI", 10), bg="#f4f5f7", fg="#444")
        self.status.pack()

        self.result = tk.Label(self, text="", font=("Segoe UI", 11, "bold"),
                               bg="#f4f5f7", fg="#1a7f37")
        self.result.pack(pady=(4, 6))

        self.button = tk.Button(self, text="НАЧАТЬ ОЧИСТКУ",
                                command=self.start_cleaning,
                                font=("Segoe UI", 12, "bold"),
                                bg="#3d7dd8", fg="white",
                                activebackground="#2f66b3",
                                relief="flat", width=24,
                                cursor="hand2")
        self.button.pack(pady=(14, 20))

    def start_cleaning(self):
        if os.name != "nt":
            messagebox.showwarning(
                "Очистка ПК",
                "Очистка доступна только на Windows.\n"
                "Текущая ОС: " + os.name)
            return

        self.button.config(state="disabled")
        self.result.config(text="")
        self.progress.start(12)
        self.status.config(text="Идёт очистка…")

        # Очистку запускаем в отдельном потоке, чтобы окно не «замерзало».
        # Результат передаётся в главный поток через очередь и after() —
        # напрямую трогать Tk из фонового потока нельзя.
        self._result_queue = queue.Queue()
        threading.Thread(target=self._clean, daemon=True).start()
        self.after(100, self._poll_result)

    def _poll_result(self):
        try:
            total, report = self._result_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_result)
            return
        self._finish(total, report)

    def _clean(self):
        report = []
        total = 0

        if self.vars["temp"].get():
            for temp_dir in {tempfile.gettempdir(), r"C:\Windows\Temp"}:
                freed = clear_folder(temp_dir)
                total += freed
                report.append(f"{temp_dir} — {self._fmt(freed)}")

        for gkey, cache_path, profile in [
            ("yandex", r"Yandex\YandexBrowser\User Data\Default\Cache",
                       r"Yandex\YandexBrowser\User Data\Default"),
            ("chrome", r"Google\Chrome\User Data\Default\Cache",
                       r"Google\Chrome\User Data\Default"),
            ("edge",   r"Microsoft\Edge\User Data\Default\Cache",
                       r"Microsoft\Edge\User Data\Default"),
        ]:
            group = self.browser_groups[gkey]
            label = group_name(gkey)

            if group.opt(gkey + "_cache").get():
                freed = clear_folder(local_app_cache(cache_path))
                total += freed
                report.append(f"{label} — кэш: {self._fmt(freed)}")

            if group.opt(gkey + "_history").get():
                n = clear_browser_history(profile)
                report.append(f"{label} — история: файлов удалено {n}")

        if self.vars["bin"].get():
            try:
                empty_recycle_bin()
                report.append("Корзина — очищена")
            except OSError:
                report.append("Корзина — уже пуста")

        self._result_queue.put((total, report))

    def _finish(self, total, report):
        self.progress.stop()
        self.button.config(state="normal")
        self.status.config(text="Готово к очистке")
        self.result.config(text=f"Освобождено: {self._fmt(total)}")
        messagebox.showinfo("Очистка завершена",
                            f"Освобождено: {self._fmt(total)}\n\n"
                            + "\n".join(report))

    @staticmethod
    def _fmt(n: int) -> str:
        if n < 1024:
            return f"{n} Б"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} КБ"
        if n < 1024 * 1024 * 1024:
            return f"{n / 1024 / 1024:.1f} МБ"
        return f"{n / 1024 / 1024 / 1024:.2f} ГБ"


if __name__ == "__main__":
    CleanerApp().mainloop()