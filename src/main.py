import sys
import os
import asyncio
import logging
import csv
from datetime import datetime
from pathlib import Path
from io import StringIO
from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
                             QWidget, QPushButton, QLineEdit, QTextEdit, QLabel,
                             QProgressBar, QFileDialog, QGroupBox, QFormLayout,
                             QMessageBox, QTabWidget, QTableWidget, QTableWidgetItem,
                             QDialog, QDialogButtonBox, QInputDialog)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QTimer
from PyQt6.QtGui import QFont, QIcon
from pyrogram import Client
from pyrogram.errors import FloodWait, UserPrivacyRestricted, ChatAdminRequired
from pyrogram.enums import UserStatus



class TelegramParserThread(QThread):
    """Поток для парсинга Telegram групп"""
    progress_signal = pyqtSignal(str)
    progress_value = pyqtSignal(int)
    finished_signal = pyqtSignal(str, list)
    error_signal = pyqtSignal(str)
    auth_code_needed = pyqtSignal(str)  # Сигнал для запроса кода
    auth_password_needed = pyqtSignal()  # Сигнал для запроса пароля

    def __init__(self, api_id, api_hash, chat_link, max_members=1000, session_name=None):
        super().__init__()
        self.api_id = api_id
        self.api_hash = api_hash
        self.chat_link = chat_link
        self.max_members = max_members
        self.client = None
        self.auth_code = None
        self.auth_password = None
        self.session_name = session_name or "telegram_parser_session"
        self.is_running = True

    def format_last_online(self, user):
        """Форматирование времени последнего посещения"""
        try:
            if not hasattr(user, 'status') or user.status is None:
                return "Скрыто"
            
            # Проверяем значение enum статуса
            if user.status == UserStatus.ONLINE:
                return "Онлайн"
            elif user.status == UserStatus.OFFLINE:
                # Пытаемся получить точное время из last_online_date
                if hasattr(user, 'last_online_date') and user.last_online_date:
                    return user.last_online_date.strftime("%Y-%m-%d %H:%M:%S")
                return "Не в сети"
            elif user.status == UserStatus.RECENTLY:
                return "Недавно"
            elif user.status == UserStatus.LAST_WEEK:
                return "На прошлой неделе"  
            elif user.status == UserStatus.LAST_MONTH:
                return "В прошлом месяце"
            elif user.status == UserStatus.LONG_TIME_AGO:
                return "Давно"
            else:
                return "Скрыто"
                
        except Exception as e:
            return "Скрыто"

    def get_user_status(self, user):
        """Получение текстового статуса пользователя"""
        return self.format_last_online(user)

    async def safe_get_chat_members(self, client, chat_id, limit=None):
        """Безопасное получение участников чата"""
        members = []
        try:
            async for member in client.get_chat_members(chat_id, limit=limit):
                if not self.is_running:  # Проверка на остановку
                    break

                members.append(member)
                await asyncio.sleep(0.1)

                # Обновляем прогресс
                if len(members) % 50 == 0:
                    self.progress_signal.emit(f"📥 Получено участников: {len(members)}")
                    self.progress_value.emit(min(len(members), limit or 1000))

        except FloodWait as e:
            if self.is_running:
                self.progress_signal.emit(f"⏳ FloodWait: ожидание {e.value} сек")
                await asyncio.sleep(e.value)
                return await self.safe_get_chat_members(client, chat_id, limit)
            else:
                return None
        except ChatAdminRequired:
            self.error_signal.emit("❌ Требуются права администратора")
            return None
        except Exception as e:
            self.error_signal.emit(f"❌ Ошибка получения участников: {str(e)}")
            return None
        return members

    async def ensure_auth(self):
        """Обеспечиваем авторизацию клиента"""
        try:
            # Проверяем авторизацию
            me = await self.client.get_me()
            self.progress_signal.emit(f"✅ Авторизован как: {me.first_name}")
            return True
        except Exception:
            self.progress_signal.emit("📱 Требуется авторизация...")

            # Запрашиваем номер телефона
            self.auth_code_needed.emit("Введите номер телефона (например: +1234567890)")
            while self.auth_code is None and self.is_running:
                await asyncio.sleep(0.1)

            if not self.is_running:
                return False

            phone = self.auth_code.strip()
            self.auth_code = None

            # Отправляем код
            self.progress_signal.emit(f"📤 Отправляем код на {phone}...")
            try:
                sent_code = await self.client.send_code(phone)
            except Exception as e:
                raise Exception(f"Не удалось отправить код: {str(e)}")

            # Запрашиваем код подтверждения
            self.auth_code_needed.emit(f"Введите код из SMS/Telegram для {phone}")
            while self.auth_code is None and self.is_running:
                await asyncio.sleep(0.1)

            if not self.is_running:
                return False

            code = self.auth_code.strip()
            self.auth_code = None

            try:
                # Пробуем войти с кодом
                await self.client.sign_in(phone, sent_code.phone_code_hash, code)
                self.progress_signal.emit("✅ Авторизация успешна")
                return True
            except Exception as sign_error:
                error_str = str(sign_error).lower()
                if "password" in error_str or "2fa" in error_str or "two-step" in error_str:
                    # Нужен пароль 2FA
                    self.progress_signal.emit("🔐 Требуется пароль 2FA...")
                    self.auth_password_needed.emit()
                    while self.auth_password is None and self.is_running:
                        await asyncio.sleep(0.1)

                    if not self.is_running:
                        return False

                    try:
                        await self.client.check_password(self.auth_password)
                        self.progress_signal.emit("✅ Авторизация с 2FA успешна")
                        return True
                    except Exception as pwd_error:
                        raise Exception(f"Неверный пароль 2FA: {str(pwd_error)}")
                else:
                    raise Exception(f"Ошибка авторизации: {str(sign_error)}")

    async def parse_group(self):
        """Основная функция парсинга"""
        old_stdin = sys.stdin
        try:
            if not self.is_running:
                return

            self.progress_signal.emit("🔄 Инициализация клиента...")

            # Перенаправляем stdin чтобы избежать консольного ввода
            sys.stdin = StringIO("")

            # Создаем или используем существующую сессию
            self.client = Client(
                self.session_name,
                api_id=int(self.api_id),
                api_hash=self.api_hash,
                in_memory=False,
                no_updates=True
            )

            self.progress_signal.emit("🔐 Подключение к Telegram...")
            await self.client.connect()

            if not self.is_running:
                return

            # Проверяем/выполняем авторизацию
            if not await self.ensure_auth():
                return

            if not self.is_running:
                return

            # Получаем информацию о чате
            self.progress_signal.emit("🔍 Получение информации о группе...")

            # Обрабатываем разные форматы ссылок
            chat_link = self.chat_link.strip()
            if chat_link.startswith("https://t.me/"):
                chat_username = chat_link.replace("https://t.me/", "")
            elif chat_link.startswith("@"):
                chat_username = chat_link[1:]  # Убираем @
            elif chat_link.startswith("t.me/"):
                chat_username = chat_link.replace("t.me/", "")
            else:
                chat_username = chat_link

            # Убираем лишние символы и параметры
            if "/" in chat_username:
                chat_username = chat_username.split("/")[0]
            if "?" in chat_username:
                chat_username = chat_username.split("?")[0]

            self.progress_signal.emit(f"🔍 Поиск группы: @{chat_username}")

            try:
                chat = await self.client.get_chat(chat_username)
            except Exception as e:
                if "USERNAME_INVALID" in str(e):
                    # Пробуем с @ в начале
                    try:
                        chat = await self.client.get_chat(f"@{chat_username}")
                    except Exception as e2:
                        raise Exception(f"Группа не найдена. Проверьте ссылку: {chat_link}\nОшибка: {str(e)}")
                else:
                    raise e

            if not self.is_running:
                return

            self.progress_signal.emit(f"📊 Группа: {chat.title}")
            self.progress_signal.emit(f"👥 Участников: {chat.members_count or 'Неизвестно'}")

            # Получаем участников
            self.progress_signal.emit("📥 Начинаю получение участников...")
            members = await self.safe_get_chat_members(self.client, chat.id, limit=self.max_members)

            if members is None or not self.is_running:
                return

            # Обрабатываем данные с расширенными полями
            parsed_data = []
            for i, member in enumerate(members):
                if not self.is_running:
                    break

                try:
                    user = member.user
                    
                    # Собираем все доступные данные в одном запросе
                    user_data = {
                        'ID': user.id,
                        'Username': user.username or '',
                        'First Name': user.first_name or '',
                        'Last Name': user.last_name or '',
                        'Phone': user.phone_number or '' if hasattr(user, 'phone_number') and user.phone_number else '',
                        'Status': self.get_user_status(user),
                        'Last Online': self.format_last_online(user),
                        'Is Bot': 'Да' if user.is_bot else 'Нет',
                        'Is Verified': 'Да' if user.is_verified else 'Нет',
                        'Is Scam': 'Да' if user.is_scam else 'Нет',
                        'Is Premium': 'Да' if user.is_premium else 'Нет'
                    }
                    
                    parsed_data.append(user_data)

                    if (i + 1) % 50 == 0:
                        self.progress_signal.emit(f"🔄 Обработано: {i + 1}/{len(members)}")
                        self.progress_value.emit(i + 1)

                except Exception as e:
                    # В случае ошибки добавляем базовые данные
                    try:
                        user = member.user
                        user_data = {
                            'ID': user.id,
                            'Username': user.username or '',
                            'First Name': user.first_name or '',
                            'Last Name': user.last_name or '',
                            'Phone': '',
                            'Status': 'Неизвестно',
                            'Last Online': 'Неизвестно',
                            'Is Bot': 'Неизвестно',
                            'Is Verified': 'Неизвестно',
                            'Is Scam': 'Неизвестно',
                            'Is Premium': 'Неизвестно'
                        }
                        parsed_data.append(user_data)
                    except:
                        continue

            if self.is_running:
                self.finished_signal.emit(chat.title, parsed_data)

        except Exception as e:
            if self.is_running:
                self.error_signal.emit(f"❌ Критическая ошибка: {str(e)}")
        finally:
            # Восстанавливаем stdin
            sys.stdin = old_stdin
            await self.cleanup()

    async def cleanup(self):
        """Очистка ресурсов"""
        if self.client:
            try:
                if self.client.is_connected:
                    await self.client.disconnect()
            except Exception as e:
                print(f"Ошибка при отключении клиента: {e}")

    def stop(self):
        """Остановка парсинга"""
        self.is_running = False

    def run(self):
        """Запуск потока"""
        try:
            asyncio.run(self.parse_group())
        except Exception as e:
            if self.is_running:
                self.error_signal.emit(f"❌ Ошибка выполнения: {str(e)}")


class TelegramParserGUI(QMainWindow):
    """Главное окно приложения"""

    def __init__(self):
        super().__init__()
        self.parser_thread = None
        self.parsed_data = []
        self.session_name = "telegram_parser_persistent"  # Постоянная сессия
        self.init_ui()
        self.setup_logging()

    def init_ui(self):
        """Инициализация интерфейса"""
        self.setWindowTitle("Telegram Group Parser v2.1 - Extended")
        self.setGeometry(100, 100, 1200, 800)  # Увеличили ширину для большего количества колонок

        # Центральный виджет
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Основной layout
        main_layout = QVBoxLayout(central_widget)

        # Создаем табы
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        # Таб настроек
        self.setup_settings_tab()

        # Таб парсинга
        self.setup_parser_tab()

        # Таб результатов
        self.setup_results_tab()

    def setup_settings_tab(self):
        """Настройки API"""
        settings_widget = QWidget()
        self.tabs.addTab(settings_widget, "⚙️ Настройки")

        layout = QVBoxLayout(settings_widget)

        # Группа API настроек
        api_group = QGroupBox("🔑 Telegram API")
        api_layout = QFormLayout(api_group)

        self.api_id_input = QLineEdit()
        self.api_id_input.setPlaceholderText("Введите API ID")
        api_layout.addRow("API ID:", self.api_id_input)

        self.api_hash_input = QLineEdit()
        self.api_hash_input.setPlaceholderText("Введите API Hash")
        api_layout.addRow("API Hash:", self.api_hash_input)

        layout.addWidget(api_group)

        # Группа настроек парсинга
        parse_group = QGroupBox("📊 Настройки парсинга")
        parse_layout = QFormLayout(parse_group)

        self.max_members_input = QLineEdit("1000")
        parse_layout.addRow("Макс. участников:", self.max_members_input)

        self.save_path_input = QLineEdit(str(Path.home() / "Desktop"))
        parse_layout.addRow("Папка сохранения:", self.save_path_input)

        browse_btn = QPushButton("📁 Обзор")
        browse_btn.clicked.connect(self.browse_save_path)
        parse_layout.addRow("", browse_btn)

        layout.addWidget(parse_group)

        # Информация о собираемых данных
        data_info_group = QGroupBox("📋 Собираемые данные")
        data_info_layout = QVBoxLayout(data_info_group)
        
        data_info_text = QLabel(
            "✅ Программа собирает следующие данные:\n"
            "• ID пользователя\n"
            "• Username (@никнейм)\n"
            "• Имя и фамилия\n"
            "• Номер телефона (если доступен)\n"
            "• Статус онлайн\n"
            "• Время последнего посещения\n"
            "• Является ли ботом\n"
            "• Верифицированный аккаунт\n"
            "• Скам аккаунт\n"
            "• Premium подписка"
        )
        data_info_text.setStyleSheet("color: #333; padding: 10px; font-size: 12px;")
        data_info_layout.addWidget(data_info_text)
        
        layout.addWidget(data_info_group)

        # Группа управления сессией
        session_group = QGroupBox("🔐 Управление сессией")
        session_layout = QVBoxLayout(session_group)

        self.clear_session_btn = QPushButton("🗑️ Очистить сессию (повторная авторизация)")
        self.clear_session_btn.clicked.connect(self.clear_session)
        session_layout.addWidget(self.clear_session_btn)

        session_info = QLabel(
            "💡 Сессия сохраняется между запусками. Очистите её, если нужно войти под другим аккаунтом."
        )
        session_info.setStyleSheet("color: #666; padding: 5px;")
        session_layout.addWidget(session_info)

        layout.addWidget(session_group)

        # Информация
        info_label = QLabel(
            "ℹ️ Для получения API ID и Hash:\n"
            "1. Перейдите на https://my.telegram.org\n"
            "2. Войдите в аккаунт\n"
            "3. Создайте приложение в разделе API development tools"
        )
        info_label.setStyleSheet("color: #666; padding: 10px;")
        layout.addWidget(info_label)

        layout.addStretch()

    def setup_parser_tab(self):
        """Таб парсинга"""
        parser_widget = QWidget()
        self.tabs.addTab(parser_widget, "🚀 Парсинг")

        layout = QVBoxLayout(parser_widget)

        # Группа ввода ссылки
        input_group = QGroupBox("🔗 Ссылка на группу")
        input_layout = QVBoxLayout(input_group)

        self.chat_link_input = QLineEdit()
        self.chat_link_input.setPlaceholderText("https://t.me/groupname, @groupname или просто groupname")
        input_layout.addWidget(self.chat_link_input)

        # Примеры ссылок
        examples_label = QLabel(
            "📝 Примеры ссылок:\n"
            "• https://t.me/python_beginners\n"
            "• @python_beginners\n"
            "• python_beginners"
        )
        examples_label.setStyleSheet("color: #666; font-size: 12px; padding: 5px;")
        input_layout.addWidget(examples_label)

        # Кнопки
        button_layout = QHBoxLayout()

        self.start_btn = QPushButton("🚀 Начать парсинг")
        self.start_btn.clicked.connect(self.start_parsing)
        self.start_btn.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; padding: 10px; font-weight: bold; }")

        self.stop_btn = QPushButton("⏹️ Остановить")
        self.stop_btn.clicked.connect(self.stop_parsing)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("QPushButton { background-color: #f44336; color: white; padding: 10px; }")

        button_layout.addWidget(self.start_btn)
        button_layout.addWidget(self.stop_btn)
        input_layout.addLayout(button_layout)

        layout.addWidget(input_group)

        # Прогресс
        progress_group = QGroupBox("📊 Прогресс")
        progress_layout = QVBoxLayout(progress_group)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        progress_layout.addWidget(self.progress_bar)

        self.status_text = QTextEdit()
        self.status_text.setMaximumHeight(200)
        self.status_text.setReadOnly(True)
        progress_layout.addWidget(self.status_text)

        layout.addWidget(progress_group)

        layout.addStretch()

    def setup_results_tab(self):
        """Таб результатов"""
        results_widget = QWidget()
        self.tabs.addTab(results_widget, "📋 Результаты")

        layout = QVBoxLayout(results_widget)

        # Кнопки управления
        button_layout = QHBoxLayout()

        self.save_csv_btn = QPushButton("💾 Сохранить CSV")
        self.save_csv_btn.clicked.connect(self.save_csv)
        self.save_csv_btn.setEnabled(False)

        self.clear_results_btn = QPushButton("🗑️ Очистить")
        self.clear_results_btn.clicked.connect(self.clear_results)

        button_layout.addWidget(self.save_csv_btn)
        button_layout.addWidget(self.clear_results_btn)
        button_layout.addStretch()

        layout.addLayout(button_layout)

        # Таблица результатов
        self.results_table = QTableWidget()
        layout.addWidget(self.results_table)

    def browse_save_path(self):
        """Выбор папки для сохранения"""
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку для сохранения")
        if folder:
            self.save_path_input.setText(folder)

    def clear_session(self):
        """Очистка сессии"""
        try:
            # Удаляем файлы сессии
            for file in Path.cwd().glob(f"{self.session_name}.*"):
                file.unlink()
            QMessageBox.information(self, "Успех",
                                    "Сессия очищена. При следующем парсинге потребуется повторная авторизация.")
        except Exception as e:
            QMessageBox.warning(self, "Ошибка", f"Не удалось очистить сессию: {str(e)}")

    def setup_logging(self):
        """Настройка логирования"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )

    def start_parsing(self):
        """Запуск парсинга"""
        # Проверка данных
        if not all([self.api_id_input.text(), self.api_hash_input.text(), self.chat_link_input.text()]):
            QMessageBox.warning(self, "Ошибка", "Заполните все обязательные поля!")
            return

        try:
            max_members = int(self.max_members_input.text())
        except ValueError:
            QMessageBox.warning(self, "Ошибка", "Введите корректное число участников!")
            return

        # Останавливаем предыдущий поток если он есть
        if self.parser_thread and self.parser_thread.isRunning():
            self.parser_thread.stop()
            self.parser_thread.wait(3000)  # Ждем до 3 секунд

        # UI изменения
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(max_members)
        self.progress_bar.setValue(0)
        self.status_text.clear()
        self.tabs.setCurrentIndex(1)  # Переключаем на таб парсинга

        # Запуск потока
        self.parser_thread = TelegramParserThread(
            self.api_id_input.text(),
            self.api_hash_input.text(),
            self.chat_link_input.text(),
            max_members,
            self.session_name
        )

        self.parser_thread.progress_signal.connect(self.update_status)
        self.parser_thread.progress_value.connect(self.progress_bar.setValue)
        self.parser_thread.finished_signal.connect(self.parsing_finished)
        self.parser_thread.error_signal.connect(self.parsing_error)
        self.parser_thread.auth_code_needed.connect(self.handle_auth_code)
        self.parser_thread.auth_password_needed.connect(self.handle_auth_password)

        self.parser_thread.start()

    def stop_parsing(self):
        """Остановка парсинга"""
        if self.parser_thread and self.parser_thread.isRunning():
            self.parser_thread.stop()
            self.update_status("⏹️ Остановка парсинга...")

            # Даем время на корректное завершение
            if not self.parser_thread.wait(5000):  # Ждем 5 секунд
                self.parser_thread.terminate()
                self.parser_thread.wait()
                self.update_status("⚠️ Парсинг принудительно остановлен")
            else:
                self.update_status("✅ Парсинг остановлен")

        self.reset_ui()

    def update_status(self, message):
        """Обновление статуса"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.status_text.append(f"[{timestamp}] {message}")

        # Автоскролл
        cursor = self.status_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.status_text.setTextCursor(cursor)

    def parsing_finished(self, chat_title, data):
        """Завершение парсинга"""
        self.parsed_data = data
        self.update_status(f"✅ Парсинг завершен! Получено {len(data)} участников")

        # Заполняем таблицу
        self.fill_results_table(data)

        # Переключаемся на результаты
        self.tabs.setCurrentIndex(2)

        self.reset_ui()
        self.save_csv_btn.setEnabled(True)

    def parsing_error(self, error_message):
        """Обработка ошибок"""
        self.update_status(error_message)
        QMessageBox.critical(self, "Ошибка парсинга", error_message)
        self.reset_ui()

    def fill_results_table(self, data):
        """Заполнение таблицы результатов"""
        if not data:
            return

        headers = list(data[0].keys())
        self.results_table.setColumnCount(len(headers))
        self.results_table.setRowCount(len(data))
        self.results_table.setHorizontalHeaderLabels(headers)

        for row, item in enumerate(data):
            for col, header in enumerate(headers):
                self.results_table.setItem(row, col, QTableWidgetItem(str(item[header])))

        self.results_table.resizeColumnsToContents()

    def save_csv(self):
        """Сохранение в CSV"""
        if not self.parsed_data:
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"telegram_members_extended_{timestamp}.csv"

        filename, _ = QFileDialog.getSaveFileName(
            self, "Сохранить CSV",
            os.path.join(self.save_path_input.text(), default_name),
            "CSV files (*.csv)"
        )

        if filename:
            try:
                with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
                    if self.parsed_data:
                        writer = csv.DictWriter(csvfile, fieldnames=self.parsed_data[0].keys())
                        writer.writeheader()
                        writer.writerows(self.parsed_data)

                QMessageBox.information(self, "Успех", f"Файл сохранен: {filename}")
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить файл: {str(e)}")

    def clear_results(self):
        """Очистка результатов"""
        self.parsed_data = []
        self.results_table.setRowCount(0)
        self.save_csv_btn.setEnabled(False)

    def handle_auth_code(self, message):
        """Обработка запроса кода авторизации"""
        code, ok = QInputDialog.getText(
            self,
            "Авторизация",
            message,
            QLineEdit.EchoMode.Normal
        )

        if ok and code:
            self.parser_thread.auth_code = code.strip()
        else:
            self.parser_thread.auth_code = ""

    def handle_auth_password(self):
        """Обработка запроса пароля 2FA"""
        password, ok = QInputDialog.getText(
            self,
            "Пароль 2FA",
            "Введите пароль двухфакторной аутентификации:",
            QLineEdit.EchoMode.Password
        )

        if ok and password:
            self.parser_thread.auth_password = password
        else:
            self.parser_thread.auth_password = ""

    def reset_ui(self):
        """Сброс UI после парсинга"""
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setVisible(False)
        self.progress_bar.setValue(0)

    def closeEvent(self, event):
        """Обработка закрытия приложения"""
        if self.parser_thread and self.parser_thread.isRunning():
            self.parser_thread.stop()
            self.parser_thread.wait(3000)
        event.accept()


def main():
    app = QApplication(sys.argv)

    # Стиль приложения
    app.setStyleSheet("""
        QMainWindow {
            background-color: #f5f5f5;
        }
        QGroupBox {
            font-weight: bold;
            border: 2px solid #ccc;
            border-radius: 5px;
            margin: 10px 0px;
            padding-top: 10px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 5px 0 5px;
        }
        QPushButton {
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
            font-weight: bold;
        }
        QPushButton:hover {
            opacity: 0.8;
        }
        QLineEdit {
            padding: 8px;
            border: 1px solid #ddd;
            border-radius: 4px;
        }
        QTextEdit {
            border: 1px solid #ddd;
            border-radius: 4px;
        }
        QTableWidget {
            gridline-color: #ddd;
            background-color: white;
        }
        QTableWidget::item {
            padding: 5px;
        }
        QTableWidget::item:selected {
            background-color: #3498db;
            color: white;
        }
    """)

    window = TelegramParserGUI()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
