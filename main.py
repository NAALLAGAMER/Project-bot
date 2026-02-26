import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
import sqlite3
import datetime
import hashlib
import requests
from typing import Dict, List, Optional
import json
import os
from dotenv import load_dotenv
import re

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS = [int(id) for id in os.getenv('8405687963', '').split(',')]  # Multiple admin support
SECRET_ADMIN_COMMAND = '/NAALLAGAMER'  # Secret admin panel command
REQUIRED_CHANNELS = []  # Will be loaded from database
MIN_WITHDRAWAL_UPI = 10  # Rs
MIN_WITHDRAWAL_GATEWAY = 1  # Rs
SUPPORT_CONTACT = "@NAALLAGAMER"  # Support username

# Database setup
def init_database():
    conn = sqlite3.connect('task_bot.db')
    c = conn.cursor()
    
    # Users table with IP tracking
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  first_name TEXT,
                  last_name TEXT,
                  balance REAL DEFAULT 0,
                  total_earned REAL DEFAULT 0,
                  total_withdrawn REAL DEFAULT 0,
                  completed_tasks INTEGER DEFAULT 0,
                  pending_tasks INTEGER DEFAULT 0,
                  joined_date TEXT,
                  last_active TEXT,
                  verified_ip TEXT,
                  is_verified INTEGER DEFAULT 0,
                  is_blocked INTEGER DEFAULT 0)''')
    
    # IP tracking table
    c.execute('''CREATE TABLE IF NOT EXISTS ip_addresses
                 (ip_address TEXT PRIMARY KEY,
                  user_id INTEGER,
                  first_seen TEXT,
                  last_seen TEXT,
                  UNIQUE(ip_address))''')
    
    # Tasks table
    c.execute('''CREATE TABLE IF NOT EXISTS tasks
                 (task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  description TEXT,
                  reward REAL,
                  requirements TEXT,
                  task_link TEXT,
                  is_active INTEGER DEFAULT 1,
                  created_date TEXT,
                  created_by INTEGER)''')
    
    # Submissions table
    c.execute('''CREATE TABLE IF NOT EXISTS submissions
                 (submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  task_id INTEGER,
                  screenshot TEXT,
                  status TEXT DEFAULT 'pending',
                  submitted_date TEXT,
                  reviewed_date TEXT,
                  reviewed_by INTEGER,
                  notes TEXT,
                  ip_address TEXT)''')
    
    # Withdrawals table
    c.execute('''CREATE TABLE IF NOT EXISTS withdrawals
                 (withdrawal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  amount REAL,
                  method TEXT,
                  account_details TEXT,
                  status TEXT DEFAULT 'pending',
                  requested_date TEXT,
                  processed_date TEXT,
                  transaction_id TEXT,
                  admin_notes TEXT)''')
    
    # Channels table
    c.execute('''CREATE TABLE IF NOT EXISTS channels
                 (channel_id TEXT PRIMARY KEY,
                  channel_name TEXT,
                  channel_type TEXT,
                  is_required INTEGER DEFAULT 1,
                  added_date TEXT,
                  added_by INTEGER)''')
    
    # Transactions log
    c.execute('''CREATE TABLE IF NOT EXISTS transactions
                 (transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  type TEXT,
                  amount REAL,
                  balance_before REAL,
                  balance_after REAL,
                  description TEXT,
                  timestamp TEXT,
                  admin_id INTEGER)''')
    
    # Gateway settings
    c.execute('''CREATE TABLE IF NOT EXISTS gateway_settings
                 (setting_key TEXT PRIMARY KEY,
                  setting_value TEXT,
                  updated_date TEXT)''')
    
    conn.commit()
    conn.close()

# Initialize database on startup
init_database()

# Helper Functions
def get_db():
    return sqlite3.connect('task_bot.db')

def log_transaction(user_id, type, amount, balance_before, balance_after, description, admin_id=None):
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO transactions 
                 (user_id, type, amount, balance_before, balance_after, description, timestamp, admin_id)
                 VALUES (?,?,?,?,?,?,?,?)''',
              (user_id, type, amount, balance_before, balance_after, description, 
               datetime.datetime.now().isoformat(), admin_id))
    conn.commit()
    conn.close()

def get_user_ip(update: Update) -> str:
    """Get user's IP address from update"""
    # Note: This requires your bot to be behind a proxy that forwards IP
    # For production, you'll need to configure your webhook properly
    if update.effective_user and update.effective_chat:
        # Try to get IP from different sources
        # This is simplified - you'll need to implement proper IP capture
        # based on your hosting environment
        return "0.0.0.0"  # Placeholder
    return "0.0.0.0"

def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> tuple:
    """Check if user is member of all required channels"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT channel_id, channel_name FROM channels WHERE is_required = 1")
    channels = c.fetchall()
    conn.close()
    
    not_joined = []
    for channel_id, channel_name in channels:
        try:
            # Remove @ if present
            if channel_id.startswith('@'):
                chat_id = channel_id
            else:
                chat_id = f"@{channel_id}"
            
            member = context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ['left', 'kicked']:
                not_joined.append(channel_name or channel_id)
        except Exception as e:
            logging.error(f"Error checking channel {channel_id}: {e}")
            # If can't check, assume not joined for security
            not_joined.append(channel_name or channel_id)
    
    return len(not_joined) == 0, not_joined

def verify_user_ip(user_id: int, ip_address: str) -> bool:
    """Verify if IP is unique for this user"""
    conn = get_db()
    c = conn.cursor()
    
    # Check if IP already exists for another user
    c.execute("SELECT user_id FROM ip_addresses WHERE ip_address = ?", (ip_address,))
    result = c.fetchone()
    
    if result and result[0] != user_id:
        conn.close()
        return False
    
    # Record or update IP
    c.execute('''INSERT OR REPLACE INTO ip_addresses (ip_address, user_id, first_seen, last_seen)
                 VALUES (?, ?, 
                         COALESCE((SELECT first_seen FROM ip_addresses WHERE ip_address = ?), ?),
                         ?)''',
              (ip_address, user_id, ip_address, datetime.datetime.now().isoformat(), 
               datetime.datetime.now().isoformat()))
    
    # Update user verification
    c.execute("UPDATE users SET verified_ip = ?, is_verified = 1 WHERE user_id = ?", 
              (ip_address, user_id))
    
    conn.commit()
    conn.close()
    return True

# User Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    conn = get_db()
    c = conn.cursor()
    
    # Check if user exists
    c.execute("SELECT * FROM users WHERE user_id = ?", (user.id,))
    existing_user = c.fetchone()
    
    if not existing_user:
        # Create new user
        c.execute('''INSERT INTO users 
                     (user_id, username, first_name, last_name, joined_date, last_active)
                     VALUES (?,?,?,?,?,?)''',
                  (user.id, user.username, user.first_name, user.last_name,
                   datetime.datetime.now().isoformat(), datetime.datetime.now().isoformat()))
        conn.commit()
    
    conn.close()
    
    # Check channel membership
    is_member, not_joined = check_channel_membership(user.id, context)
    
    if not is_member:
        # Create channel join buttons
        keyboard = []
        for channel in not_joined:
            keyboard.append([InlineKeyboardButton(f"Join {channel}", url=f"https://t.me/{channel.replace('@', '')}")])
        keyboard.append([InlineKeyboardButton("✅ I've Joined", callback_data="verify_channels")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🔒 *Channel Verification Required*\n\n"
            "To use this bot, you must join our channels:\n"
            f"{chr(10).join(['• ' + ch for ch in not_joined])}\n\n"
            "After joining all channels, click the verification button below.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Check IP verification
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT is_verified, verified_ip FROM users WHERE user_id = ?", (user.id,))
    is_verified, verified_ip = c.fetchone()
    conn.close()
    
    if not is_verified:
        # Request IP verification
        keyboard = [[InlineKeyboardButton("🌐 Verify IP Address", callback_data="verify_ip")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🔐 *IP Verification Required*\n\n"
            "For security reasons, we need to verify your IP address.\n"
            "This ensures one account per user and prevents fraud.\n\n"
            "Click the button below to verify.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Fully verified user
    await show_main_menu(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu with buttons"""
    user = update.effective_user
    
    # Get user data
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT balance, completed_tasks, pending_tasks FROM users WHERE user_id = ?", (user.id,))
    balance, completed, pending = c.fetchone()
    conn.close()
    
    # Create main menu keyboard
    keyboard = [
        [KeyboardButton("📋 Available Tasks"), KeyboardButton("💰 My Balance")],
        [KeyboardButton("📊 My Profile"), KeyboardButton("💳 Withdraw")],
        [KeyboardButton("📞 Support"), KeyboardButton("📜 History")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    welcome_text = (
        f"👋 Welcome back, {user.first_name}!\n\n"
        f"💰 Balance: ₹{balance:.2f}\n"
        f"✅ Completed Tasks: {completed}\n"
        f"⏳ Pending Tasks: {pending}\n\n"
        "Use the buttons below to navigate."
    )
    
    if update.message:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup)
    else:
        await update.callback_query.message.reply_text(welcome_text, reply_markup=reply_markup)

async def handle_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle verification callbacks"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    
    if query.data == "verify_channels":
        # Recheck channel membership
        is_member, not_joined = check_channel_membership(user.id, context)
        
        if is_member:
            await query.edit_message_text(
                "✅ *Channel Verification Successful!*\n\n"
                "Now let's verify your IP address.",
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Ask for IP verification
            keyboard = [[InlineKeyboardButton("🌐 Verify IP", callback_data="verify_ip")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.reply_text(
                "Click below for IP verification:",
                reply_markup=reply_markup
            )
        else:
            # Still not joined
            keyboard = []
            for channel in not_joined:
                keyboard.append([InlineKeyboardButton(f"Join {channel}", url=f"https://t.me/{channel.replace('@', '')}")])
            keyboard.append([InlineKeyboardButton("✅ I've Joined", callback_data="verify_channels")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "❌ *Verification Failed*\n\n"
                "You haven't joined all required channels yet.\n"
                f"Missing: {', '.join(not_joined)}",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
    
    elif query.data == "verify_ip":
        # Get user IP and verify
        ip_address = get_user_ip(update)
        
        if ip_address == "0.0.0.0":
            # Couldn't get IP - ask user to visit a link
            await query.edit_message_text(
                "⚠️ *IP Detection Issue*\n\n"
                "Please visit the following link to verify:\n"
                "https://your-verification-site.com/verify",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Verify IP
        if verify_user_ip(user.id, ip_address):
            await query.edit_message_text(
                "✅ *Verification Complete!*\n\n"
                "You now have full access to the bot.",
                parse_mode=ParseMode.MARKDOWN
            )
            await show_main_menu(update, context)
        else:
            await query.edit_message_text(
                "❌ *Verification Failed*\n\n"
                "This IP address is already associated with another account.\n"
                "Multiple accounts from the same IP are not allowed.",
                parse_mode=ParseMode.MARKDOWN
            )

async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available tasks"""
    user = update.effective_user
    
    # Verify user is verified
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT is_verified, is_blocked FROM users WHERE user_id = ?", (user.id,))
    result = c.fetchone()
    
    if not result or not result[0] or result[1]:
        await update.message.reply_text("❌ Please complete verification first using /start")
        return
    
    # Get active tasks
    c.execute("SELECT task_id, description, reward, requirements, task_link FROM tasks WHERE is_active = 1")
    tasks = c.fetchall()
    conn.close()
    
    if not tasks:
        await update.message.reply_text("📭 No tasks available at the moment. Check back later!")
        return
    
    for task_id, desc, reward, req, link in tasks:
        task_text = (
            f"📌 *Task #{task_id}*\n\n"
            f"📝 {desc}\n"
            f"💰 Reward: ₹{reward:.2f}\n"
            f"📋 Requirements: {req or 'None'}\n"
            f"🔗 Link: {link or 'N/A'}\n\n"
            f"To submit: /submit_{task_id}"
        )
        
        keyboard = [[InlineKeyboardButton("✅ Submit Task", callback_data=f"submit_{task_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(task_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def submit_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle task submission"""
    # Check if it's a callback or command
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        task_id = int(query.data.split('_')[1])
        user = query.from_user
        message = query.message
    else:
        # Command format: /submit_123
        if not context.args and not update.message.text.startswith('/submit_'):
            await update.message.reply_text("Please use /submit_TASKID or click submit button on a task")
            return
        
        if update.message.text.startswith('/submit_'):
            try:
                task_id = int(update.message.text.split('_')[1])
            except:
                await update.message.reply_text("Invalid task ID format")
                return
        else:
            await update.message.reply_text("Please use /submit_TASKID")
            return
        
        user = update.effective_user
        message = update.message
    
    # Verify user
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT is_verified, is_blocked FROM users WHERE user_id = ?", (user.id,))
    result = c.fetchone()
    
    if not result or not result[0] or result[1]:
        await message.reply_text("❌ Please complete verification first using /start")
        return
    
    # Check if task exists and is active
    c.execute("SELECT * FROM tasks WHERE task_id = ? AND is_active = 1", (task_id,))
    task = c.fetchone()
    if not task:
        await message.reply_text("❌ Task not found or inactive")
        return
    
    # Check if already submitted pending
    c.execute('''SELECT * FROM submissions 
                 WHERE user_id = ? AND task_id = ? AND status = 'pending' ''', 
              (user.id, task_id))
    if c.fetchone():
        await message.reply_text("⚠️ You already have a pending submission for this task")
        return
    
    conn.close()
    
    # Ask for screenshot
    context.user_data['pending_submission'] = task_id
    await message.reply_text(
        f"📸 Please send a screenshot of your task completion for Task #{task_id}\n\n"
        "Make sure the screenshot clearly shows the completion proof."
    )

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle screenshot submission"""
    if 'pending_submission' not in context.user_data:
        await update.message.reply_text("Please start a task submission first using /tasks")
        return
    
    task_id = context.user_data['pending_submission']
    user = update.effective_user
    photo = update.message.photo[-1]  # Get the largest photo
    
    # Get user IP
    ip_address = get_user_ip(update)
    
    # Save submission
    conn = get_db()
    c = conn.cursor()
    
    # Check if already completed this task
    c.execute('''SELECT * FROM submissions 
                 WHERE user_id = ? AND task_id = ? AND status = 'approved' ''', 
              (user.id, task_id))
    if c.fetchone():
        await update.message.reply_text("❌ You have already completed this task")
        del context.user_data['pending_submission']
        conn.close()
        return
    
    # Save submission
    c.execute('''INSERT INTO submissions 
                 (user_id, task_id, screenshot, status, submitted_date, ip_address)
                 VALUES (?,?,?,?,?,?)''',
              (user.id, task_id, photo.file_id, 'pending', 
               datetime.datetime.now().isoformat(), ip_address))
    
    # Update user pending tasks count
    c.execute('''UPDATE users SET pending_tasks = pending_tasks + 1, last_active = ?
                 WHERE user_id = ?''',
              (datetime.datetime.now().isoformat(), user.id))
    
    conn.commit()
    
    # Get submission ID
    submission_id = c.lastrowid
    
    # Get task details for admin notification
    c.execute("SELECT description, reward FROM tasks WHERE task_id = ?", (task_id,))
    task_desc, task_reward = c.fetchone()
    conn.close()
    
    # Notify admins
    for admin_id in ADMIN_IDS:
        try:
            admin_text = (
                f"📥 *New Task Submission*\n\n"
                f"📋 Submission ID: #{submission_id}\n"
                f"👤 User: {user.first_name} (@{user.username})\n"
                f"🆔 User ID: {user.id}\n"
                f"📝 Task #{task_id}: {task_desc}\n"
                f"💰 Reward: ₹{task_reward:.2f}\n"
                f"🕐 Submitted: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"🌐 IP: {ip_address}\n\n"
                f"Use /approve_{submission_id} or /reject_{submission_id}"
            )
            
            # Send photo to admin
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo.file_id,
                caption=admin_text,
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
    
    await update.message.reply_text(
        "✅ *Task Submitted Successfully!*\n\n"
        "Your submission is pending admin approval.\n"
        "You'll be notified once it's reviewed.",
        parse_mode=ParseMode.MARKDOWN
    )
    
    del context.user_data['pending_submission']

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user profile"""
    user = update.effective_user
    
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT balance, total_earned, total_withdrawn, completed_tasks, 
                        pending_tasks, joined_date, is_verified
                 FROM users WHERE user_id = ?''', (user.id,))
    balance, total_earned, total_withdrawn, completed, pending, joined, verified = c.fetchone()
    
    # Get recent transactions
    c.execute('''SELECT type, amount, description, timestamp 
                 FROM transactions WHERE user_id = ? 
                 ORDER BY timestamp DESC LIMIT 5''', (user.id,))
    transactions = c.fetchall()
    conn.close()
    
    profile_text = (
        f"👤 *Profile - {user.first_name}*\n\n"
        f"🆔 User ID: `{user.id}`\n"
        f"📛 Username: @{user.username or 'Not set'}\n"
        f"🔐 Verified: {'✅' if verified else '❌'}\n"
        f"📅 Joined: {joined[:10] if joined else 'N/A'}\n\n"
        f"💰 *Financial Stats*\n"
        f"Current Balance: ₹{balance:.2f}\n"
        f"Total Earned: ₹{total_earned:.2f}\n"
        f"Total Withdrawn: ₹{total_withdrawn:.2f}\n\n"
        f"📊 *Task Stats*\n"
        f"Completed Tasks: {completed}\n"
        f"Pending Tasks: {pending}\n\n"
        f"📜 *Recent Transactions*\n"
    )
    
    if transactions:
        for t in transactions:
            emoji = "➕" if t[0] == 'credit' else "➖" if t[0] == 'debit' else "💳"
            profile_text += f"{emoji} {t[2][:30]}... ₹{t[1]:.2f} ({t[3][:10]})\n"
    else:
        profile_text += "No recent transactions\n"
    
    await update.message.reply_text(profile_text, parse_mode=ParseMode.MARKDOWN)

async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle withdrawal request"""
    user = update.effective_user
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT balance, is_verified, is_blocked FROM users WHERE user_id = ?", (user.id,))
    balance, verified, blocked = c.fetchone()
    
    if not verified or blocked:
        await update.message.reply_text("❌ Please complete verification first using /start")
        conn.close()
        return
    
    conn.close()
    
    # Show withdrawal options
    keyboard = [
        [InlineKeyboardButton(f"💳 UPI (Min ₹{MIN_WITHDRAWAL_UPI})", callback_data="withdraw_upi")],
        [InlineKeyboardButton(f"⚡ Instant Gateway (Min ₹{MIN_WITHDRAWAL_GATEWAY})", callback_data="withdraw_gateway")],
        [InlineKeyboardButton("◀️ Back", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"💸 *Withdraw Funds*\n\n"
        f"Your Balance: ₹{balance:.2f}\n\n"
        f"Choose withdrawal method:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle withdrawal method selection"""
    query = update.callback_query
    await query.answer()
    
    method = query.data.split('_')[1]
    context.user_data['withdraw_method'] = method
    
    if method == 'gateway':
        min_amount = MIN_WITHDRAWAL_GATEWAY
        method_name = "Instant Gateway"
    else:
        min_amount = MIN_WITHDRAWAL_UPI
        method_name = "UPI"
    
    # Get user balance
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (query.from_user.id,))
    balance = c.fetchone()[0]
    conn.close()
    
    await query.edit_message_text(
        f"💳 *{method_name} Withdrawal*\n\n"
        f"Your Balance: ₹{balance:.2f}\n"
        f"Minimum: ₹{min_amount}\n\n"
        f"Please enter the amount you want to withdraw:\n"
        f"(Type a number between {min_amount} and {balance:.2f})",
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data['awaiting_withdraw_amount'] = True

async def handle_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle withdrawal amount input"""
    if 'awaiting_withdraw_amount' not in context.user_data:
        return
    
    user = update.effective_user
    method = context.user_data.get('withdraw_method')
    
    try:
        amount = float(update.message.text)
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number")
        return
    
    # Check minimum based on method
    min_amount = MIN_WITHDRAWAL_GATEWAY if method == 'gateway' else MIN_WITHDRAWAL_UPI
    
    if amount < min_amount:
        await update.message.reply_text(f"❌ Minimum withdrawal amount is ₹{min_amount}")
        return
    
    # Check balance
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user.id,))
    balance = c.fetchone()[0]
    
    if amount > balance:
        await update.message.reply_text(f"❌ Insufficient balance. Your balance: ₹{balance:.2f}")
        conn.close()
        return
    
    if method == 'gateway':
        # For gateway, we need account details
        await update.message.reply_text(
            "Please enter your gateway account details (e.g., Phone number/Email):"
        )
        context.user_data['withdraw_amount'] = amount
        context.user_data['awaiting_gateway_details'] = True
        conn.close()
    else:
        # For UPI, we need UPI ID
        await update.message.reply_text(
            "Please enter your UPI ID (e.g., name@okhdfcbank):"
        )
        context.user_data['withdraw_amount'] = amount
        context.user_data['awaiting_upi_details'] = True
        conn.close()
    
    del context.user_data['awaiting_withdraw_amount']

async def handle_withdraw_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle withdrawal account details"""
    if 'awaiting_upi_details' not in context.user_data and 'awaiting_gateway_details' not in context.user_data:
        return
    
    user = update.effective_user
    amount = context.user_data.get('withdraw_amount')
    
    if 'awaiting_upi_details' in context.user_data:
        method = 'upi'
        account_details = update.message.text
        # Simple UPI validation
        if '@' not in account_details:
            await update.message.reply_text("❌ Please enter a valid UPI ID (e.g., name@bank)")
            return
        del context.user_data['awaiting_upi_details']
    else:
        method = 'gateway'
        account_details = update.message.text
        del context.user_data['awaiting_gateway_details']
    
    # Process withdrawal
    conn = get_db()
    c = conn.cursor()
    
    # Get current balance
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user.id,))
    balance_before = c.fetchone()[0]
    
    # Create withdrawal request
    c.execute('''INSERT INTO withdrawals 
                 (user_id, amount, method, account_details, status, requested_date)
                 VALUES (?,?,?,?,?,?)''',
              (user.id, amount, method, account_details, 'pending', 
               datetime.datetime.now().isoformat()))
    
    withdrawal_id = c.lastrowid
    
    # Deduct balance immediately (will be refunded if rejected)
    balance_after = balance_before - amount
    c.execute("UPDATE users SET balance = ?, total_withdrawn = total_withdrawn + ? WHERE user_id = ?",
              (balance_after, amount, user.id))
    
    # Log transaction
    log_transaction(user.id, 'withdrawal_request', amount, balance_before, balance_after,
                   f"Withdrawal request #{withdrawal_id} via {method.upper()}")
    
    conn.commit()
    conn.close()
    
    # Notify admins
    for admin_id in ADMIN_IDS:
        try:
            admin_text = (
                f"💰 *New Withdrawal Request*\n\n"
                f"📋 Request ID: #{withdrawal_id}\n"
                f"👤 User: {user.first_name} (@{user.username})\n"
                f"🆔 User ID: {user.id}\n"
                f"💵 Amount: ₹{amount:.2f}\n"
                f"💳 Method: {method.upper()}\n"
                f"📝 Details: {account_details}\n"
                f"🕐 Requested: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                f"Use /approve_withdraw_{withdrawal_id} or /reject_withdraw_{withdrawal_id}"
            )
            await context.bot.send_message(chat_id=admin_id, text=admin_text, parse_mode=ParseMode.MARKDOWN)
        except:
            pass
    
    await update.message.reply_text(
        f"✅ *Withdrawal Request Submitted*\n\n"
        f"Request ID: #{withdrawal_id}\n"
        f"Amount: ₹{amount:.2f}\n"
        f"Method: {method.upper()}\n\n"
        f"Your request is pending admin approval.\n"
        f"You'll be notified once processed.",
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Clear context
    del context.user_data['withdraw_amount']
    del context.user_data['withdraw_method']

# Admin Commands
async def secret_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Secret admin panel access"""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized access")
        return
    
    # Show admin panel
    keyboard = [
        [InlineKeyboardButton("➕ Add Task", callback_data="admin_add_task")],
        [InlineKeyboardButton("❌ Remove Task", callback_data="admin_remove_task")],
        [InlineKeyboardButton("📋 All Tasks", callback_data="admin_list_tasks")],
        [InlineKeyboardButton("👥 All Users", callback_data="admin_list_users")],
        [InlineKeyboardButton("💳 Pending Withdrawals", callback_data="admin_pending_withdrawals")],
        [InlineKeyboardButton("📝 Pending Submissions", callback_data="admin_pending_submissions")],
        [InlineKeyboardButton("💰 Financial Stats", callback_data="admin_financial_stats")],
        [InlineKeyboardButton("➕ Add Channel", callback_data="admin_add_channel")],
        [InlineKeyboardButton("❌ Remove Channel", callback_data="admin_remove_channel")],
        [InlineKeyboardButton("📊 System Stats", callback_data="admin_system_stats")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🔐 *Admin Control Panel*\n\n"
        "Welcome to the secret admin panel.\n"
        "Select an option below:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def admin_add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start add task process"""
    query = update.callback_query
    await query.answer()
    
    context.user_data['admin_action'] = 'add_task'
    await query.edit_message_text(
        "📝 *Add New Task*\n\n"
        "Please send the task details in this format:\n\n"
        "Description | Reward | Requirements | Link\n\n"
        "Example:\n"
        "Join and post comment | 5 | Must join channel | https://t.me/...\n\n"
        "Requirements and Link are optional.",
        parse_mode=ParseMode.MARKDOWN
    )

async def admin_handle_task_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle task addition"""
    if context.user_data.get('admin_action') != 'add_task':
        return
    
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    text = update.message.text
    
    # Parse input
    parts = text.split('|')
    if len(parts) < 2:
        await update.message.reply_text(
            "❌ Invalid format. Please use:\n"
            "Description | Reward | Requirements | Link"
        )
        return
    
    description = parts[0].strip()
    try:
        reward = float(parts[1].strip())
    except:
        await update.message.reply_text("❌ Invalid reward amount")
        return
    
    requirements = parts[2].strip() if len(parts) > 2 else ""
    task_link = parts[3].strip() if len(parts) > 3 else ""
    
    # Save to database
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO tasks 
                 (description, reward, requirements, task_link, created_date, created_by, is_active)
                 VALUES (?,?,?,?,?,?,?)''',
              (description, reward, requirements, task_link, 
               datetime.datetime.now().isoformat(), user.id, 1))
    
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    
    # Notify all users about new task
    await notify_users_new_task(context, task_id, description, reward)
    
    await update.message.reply_text(f"✅ Task #{task_id} added successfully!")
    
    # Clear admin action
    del context.user_data['admin_action']

async def notify_users_new_task(context, task_id, description, reward):
    """Notify all users about new task"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE is_verified = 1 AND is_blocked = 0")
    users = c.fetchall()
    conn.close()
    
    notification = (
        f"🆕 *New Task Available!*\n\n"
        f"📌 Task #{task_id}\n"
        f"📝 {description}\n"
        f"💰 Reward: ₹{reward:.2f}\n\n"
        f"Check /tasks to view and submit!"
    )
    
    for (user_id,) in users:
        try:
            await context.bot.send_message(chat_id=user_id, text=notification, parse_mode=ParseMode.MARKDOWN)
        except:
            pass

async def admin_pending_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending submissions"""
    query = update.callback_query
    await query.answer()
    
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT s.submission_id, s.user_id, s.task_id, s.submitted_date,
                        u.username, t.description, t.reward
                 FROM submissions s
                 JOIN users u ON s.user_id = u.user_id
                 JOIN tasks t ON s.task_id = t.task_id
                 WHERE s.status = 'pending'
                 ORDER BY s.submitted_date''')
    submissions = c.fetchall()
    conn.close()
    
    if not submissions:
        await query.edit_message_text("📭 No pending submissions")
        return
    
    text = "📝 *Pending Submissions*\n\n"
    for sub in submissions[:10]:  # Show first 10
        text += (
            f"ID: #{sub[0]}\n"
            f"User: {sub[4] or 'N/A'} (ID: {sub[1]})\n"
            f"Task #{sub[2]}: {sub[5][:30]}...\n"
            f"Reward: ₹{sub[6]:.2f}\n"
            f"Date: {sub[3][:16]}\n"
            f"Approve: /approve_{sub[0]}\n"
            f"Reject: /reject_{sub[0]}\n\n"
        )
    
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

async def admin_approve_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve a submission"""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    # Parse submission ID
    if not context.args and not update.message.text.startswith('/approve_'):
        await update.message.reply_text("Usage: /approve_SUBMISSION_ID")
        return
    
    if update.message.text.startswith('/approve_'):
        try:
            submission_id = int(update.message.text.split('_')[1])
        except:
            await update.message.reply_text("Invalid submission ID")
            return
    else:
        try:
            submission_id = int(context.args[0])
        except:
            await update.message.reply_text("Invalid submission ID")
            return
    
    conn = get_db()
    c = conn.cursor()
    
    # Get submission details
    c.execute('''SELECT s.user_id, s.task_id, t.reward, u.balance
                 FROM submissions s
                 JOIN tasks t ON s.task_id = t.task_id
                 JOIN users u ON s.user_id = u.user_id
                 WHERE s.submission_id = ? AND s.status = 'pending' ''', 
              (submission_id,))
    result = c.fetchone()
    
    if not result:
        await update.message.reply_text("Submission not found or already processed")
        conn.close()
        return
    
    user_id, task_id, reward, current_balance = result
    
    # Update submission status
    c.execute('''UPDATE submissions 
                 SET status = 'approved', reviewed_date = ?, reviewed_by = ?
                 WHERE submission_id = ?''',
              (datetime.datetime.now().isoformat(), user.id, submission_id))
    
    # Update user balance and stats
    new_balance = current_balance + reward
    c.execute('''UPDATE users 
                 SET balance = ?, total_earned = total_earned + ?, 
                     completed_tasks = completed_tasks + 1, pending_tasks = pending_tasks - 1
                 WHERE user_id = ?''',
              (new_balance, reward, user_id))
    
    # Log transaction
    log_transaction(user_id, 'credit', reward, current_balance, new_balance,
                   f"Task #{task_id} approved")
    
    conn.commit()
    conn.close()
    
    # Notify user
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ *Task Approved!*\n\n"
                 f"Your submission for Task #{task_id} has been approved.\n"
                 f"💰 Reward: ₹{reward:.2f} added to your balance.\n"
                 f"New Balance: ₹{new_balance:.2f}",
            parse_mode=ParseMode.MARKDOWN
        )
    except:
        pass
    
    await update.message.reply_text(f"✅ Submission #{submission_id} approved. ₹{reward:.2f} credited to user.")

async def admin_reject_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reject a submission"""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    # Parse submission ID
    if not context.args and not update.message.text.startswith('/reject_'):
        await update.message.reply_text("Usage: /reject_SUBMISSION_ID [reason]")
        return
    
    reason = " ".join(context.args[1:]) if context.args and len(context.args) > 1 else "No reason provided"
    
    if update.message.text.startswith('/reject_'):
        try:
            parts = update.message.text.split('_')
            submission_id = int(parts[1].split()[0])
            if len(parts) > 2 or ' ' in update.message.text:
                reason = update.message.text.split(' ', 1)[1] if ' ' in update.message.text else "No reason provided"
        except:
            await update.message.reply_text("Invalid submission ID")
            return
    else:
        try:
            submission_id = int(context.args[0])
        except:
            await update.message.reply_text("Invalid submission ID")
            return
    
    conn = get_db()
    c = conn.cursor()
    
    # Get submission details
    c.execute('''SELECT s.user_id, s.task_id, u.pending_tasks
                 FROM submissions s
                 JOIN users u ON s.user_id = u.user_id
                 WHERE s.submission_id = ? AND s.status = 'pending' ''', 
              (submission_id,))
    result = c.fetchone()
    
    if not result:
        await update.message.reply_text("Submission not found or already processed")
        conn.close()
        return
    
    user_id, task_id, pending_tasks = result
    
    # Update submission status
    c.execute('''UPDATE submissions 
                 SET status = 'rejected', reviewed_date = ?, reviewed_by = ?, notes = ?
                 WHERE submission_id = ?''',
              (datetime.datetime.now().isoformat(), user.id, reason, submission_id))
    
    # Update user pending tasks
    c.execute('''UPDATE users 
                 SET pending_tasks = pending_tasks - 1
                 WHERE user_id = ?''', (user_id,))
    
    conn.commit()
    conn.close()
    
    # Notify user
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ *Task Rejected*\n\n"
                 f"Your submission for Task #{task_id} has been rejected.\n"
                 f"Reason: {reason}\n\n"
                 f"Please review the requirements and submit again.",
            parse_mode=ParseMode.MARKDOWN
        )
    except:
        pass
    
    await update.message.reply_text(f"❌ Submission #{submission_id} rejected. User notified.")

async def admin_pending_withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending withdrawals"""
    query = update.callback_query
    await query.answer()
    
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT w.withdrawal_id, w.user_id, w.amount, w.method, 
                        w.account_details, w.requested_date, u.username
                 FROM withdrawals w
                 JOIN users u ON w.user_id = u.user_id
                 WHERE w.status = 'pending'
                 ORDER BY w.requested_date''')
    withdrawals = c.fetchall()
    conn.close()
    
    if not withdrawals:
        await query.edit_message_text("📭 No pending withdrawals")
        return
    
    text = "💳 *Pending Withdrawals*\n\n"
    for w in withdrawals:
        text += (
            f"ID: #{w[0]}\n"
            f"User: {w[6] or 'N/A'} (ID: {w[1]})\n"
            f"Amount: ₹{w[2]:.2f}\n"
            f"Method: {w[3].upper()}\n"
            f"Details: {w[4]}\n"
            f"Date: {w[5][:16]}\n"
            f"Approve: /approve_withdraw_{w[0]}\n"
            f"Reject: /reject_withdraw_{w[0]}\n\n"
        )
    
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

async def admin_approve_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve a withdrawal"""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    # Parse withdrawal ID
    if not context.args and not update.message.text.startswith('/approve_withdraw_'):
        await update.message.reply_text("Usage: /approve_withdraw_WITHDRAWAL_ID [txn_id]")
        return
    
    txn_id = " ".join(context.args[1:]) if context.args and len(context.args) > 1 else "MANUAL"
    
    if update.message.text.startswith('/approve_withdraw_'):
        try:
            parts = update.message.text.split('_')
            withdrawal_id = int(parts[2].split()[0])
            if len(parts) > 3 or ' ' in update.message.text:
                txn_id = update.message.text.split(' ', 1)[1] if ' ' in update.message.text else "MANUAL"
        except:
            await update.message.reply_text("Invalid withdrawal ID")
            return
    else:
        try:
            withdrawal_id = int(context.args[0])
        except:
            await update.message.reply_text("Invalid withdrawal ID")
            return
    
    conn = get_db()
    c = conn.cursor()
    
    # Get withdrawal details
    c.execute('''SELECT user_id, amount, method, account_details
                 FROM withdrawals
                 WHERE withdrawal_id = ? AND status = 'pending' ''', 
              (withdrawal_id,))
    result = c.fetchone()
    
    if not result:
        await update.message.reply_text("Withdrawal not found or already processed")
        conn.close()
        return
    
    user_id, amount, method, account_details = result
    
    # Update withdrawal status
    c.execute('''UPDATE withdrawals 
                 SET status = 'completed', processed_date = ?, transaction_id = ?
                 WHERE withdrawal_id = ?''',
              (datetime.datetime.now().isoformat(), txn_id, withdrawal_id))
    
    conn.commit()
    conn.close()
    
    # Notify user
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ *Withdrawal Completed!*\n\n"
                 f"Your withdrawal of ₹{amount:.2f} via {method.upper()} has been processed.\n"
                 f"Transaction ID: {txn_id}\n\n"
                 f"Thank you for using our service!",
            parse_mode=ParseMode.MARKDOWN
        )
    except:
        pass
    
    await update.message.reply_text(f"✅ Withdrawal #{withdrawal_id} approved and marked as completed.")

async def admin_reject_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reject a withdrawal and refund"""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    # Parse withdrawal ID
    if not context.args and not update.message.text.startswith('/reject_withdraw_'):
        await update.message.reply_text("Usage: /reject_withdraw_WITHDRAWAL_ID [reason]")
        return
    
    reason = " ".join(context.args[1:]) if context.args and len(context.args) > 1 else "No reason provided"
    
    if update.message.text.startswith('/reject_withdraw_'):
        try:
            parts = update.message.text.split('_')
            withdrawal_id = int(parts[2].split()[0])
            if len(parts) > 3 or ' ' in update.message.text:
                reason = update.message.text.split(' ', 1)[1] if ' ' in update.message.text else "No reason provided"
        except:
            await update.message.reply_text("Invalid withdrawal ID")
            return
    else:
        try:
            withdrawal_id = int(context.args[0])
        except:
            await update.message.reply_text("Invalid withdrawal ID")
            return
    
    conn = get_db()
    c = conn.cursor()
    
    # Get withdrawal details
    c.execute('''SELECT user_id, amount, method
                 FROM withdrawals
                 WHERE withdrawal_id = ? AND status = 'pending' ''', 
              (withdrawal_id,))
    result = c.fetchone()
    
    if not result:
        await update.message.reply_text("Withdrawal not found or already processed")
        conn.close()
        return
    
    user_id, amount, method = result
    
    # Get current balance
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    current_balance = c.fetchone()[0]
    
    # Refund amount
    new_balance = current_balance + amount
    
    # Update withdrawal status
    c.execute('''UPDATE withdrawals 
                 SET status = 'rejected', processed_date = ?, admin_notes = ?
                 WHERE withdrawal_id = ?''',
              (datetime.datetime.now().isoformat(), reason, withdrawal_id))
    
    # Update user balance
    c.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
    
    # Log transaction
    log_transaction(user_id, 'refund', amount, current_balance, new_balance,
                   f"Withdrawal #{withdrawal_id} rejected - {reason}", user.id)
    
    conn.commit()
    conn.close()
    
    # Notify user
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ *Withdrawal Rejected*\n\n"
                 f"Your withdrawal request for ₹{amount:.2f} has been rejected.\n"
                 f"Reason: {reason}\n\n"
                 f"Amount has been refunded to your balance.\n"
                 f"New Balance: ₹{new_balance:.2f}",
            parse_mode=ParseMode.MARKDOWN
        )
    except:
        pass
    
    await update.message.reply_text(f"❌ Withdrawal #{withdrawal_id} rejected. ₹{amount:.2f} refunded to user.")

async def admin_financial_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show financial statistics"""
    query = update.callback_query
    await query.answer()
    
    conn = get_db()
    c = conn.cursor()
    
    # Total user balance
    c.execute("SELECT SUM(balance) FROM users")
    total_user_balance = c.fetchone()[0] or 0
    
    # Total paid out
    c.execute("SELECT SUM(amount) FROM withdrawals WHERE status = 'completed'")
    total_paid = c.fetchone()[0] or 0
    
    # Total pending withdrawals
    c.execute("SELECT SUM(amount) FROM withdrawals WHERE status = 'pending'")
    total_pending = c.fetchone()[0] or 0
    
    # Total task rewards given
    c.execute('''SELECT SUM(t.reward) 
                 FROM submissions s
                 JOIN tasks t ON s.task_id = t.task_id
                 WHERE s.status = 'approved' ''')
    total_rewards = c.fetchone()[0] or 0
    
    # Total users
    c.execute("SELECT COUNT(*) FROM users WHERE is_verified = 1")
    total_users = c.fetchone()[0]
    
    # Total tasks
    c.execute("SELECT COUNT(*) FROM tasks WHERE is_active = 1")
    total_tasks = c.fetchone()[0]
    
    # Recent transactions
    c.execute('''SELECT type, SUM(amount) 
                 FROM transactions 
                 WHERE date(timestamp) = date('now')
                 GROUP BY type''')
    today_txns = c.fetchall()
    
    conn.close()
    
    text = (
        "💰 *Financial Statistics*\n\n"
        f"👥 Total Verified Users: {total_users}\n"
        f"📋 Active Tasks: {total_tasks}\n\n"
        f"💵 *Balances*\n"
        f"Total User Balance: ₹{total_user_balance:.2f}\n"
        f"Total Paid Out: ₹{total_paid:.2f}\n"
        f"Pending Withdrawals: ₹{total_pending:.2f}\n\n"
        f"📊 *Task Economy*\n"
        f"Total Rewards Given: ₹{total_rewards:.2f}\n\n"
        f"📈 *Today's Activity*\n"
    )
    
    credit_today = sum(amt for type, amt in today_txns if type == 'credit')
    debit_today = sum(amt for type, amt in today_txns if type in ['debit', 'withdrawal_request'])
    
    text += f"Credits: ₹{credit_today:.2f}\n"
    text += f"Debits: ₹{debit_today:.2f}\n"
    text += f"Net: ₹{credit_today - debit_today:.2f}\n"
    
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

async def admin_system_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show system statistics"""
    query = update.callback_query
    await query.answer()
    
    conn = get_db()
    c = conn.cursor()
    
    # User stats
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM users WHERE is_verified = 1")
    verified_users = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM users WHERE is_blocked = 1")
    blocked_users = c.fetchone()[0]
    
    # Task stats
    c.execute("SELECT COUNT(*) FROM tasks WHERE is_active = 1")
    active_tasks = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM submissions WHERE status = 'pending'")
    pending_subs = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM submissions WHERE status = 'approved'")
    approved_subs = c.fetchone()[0]
    
    # Withdrawal stats
    c.execute("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending'")
    pending_withdrawals = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM withdrawals WHERE status = 'completed'")
    completed_withdrawals = c.fetchone()[0]
    
    # Channel stats
    c.execute("SELECT COUNT(*) FROM channels")
    total_channels = c.fetchone()[0]
    
    conn.close()
    
    text = (
        "📊 *System Statistics*\n\n"
        f"👥 *Users*\n"
        f"Total: {total_users}\n"
        f"Verified: {verified_users}\n"
        f"Blocked: {blocked_users}\n\n"
        f"📋 *Tasks*\n"
        f"Active: {active_tasks}\n"
        f"Pending Submissions: {pending_subs}\n"
        f"Approved Submissions: {approved_subs}\n\n"
        f"💳 *Withdrawals*\n"
        f"Pending: {pending_withdrawals}\n"
        f"Completed: {completed_withdrawals}\n\n"
        f"📢 *Channels*\n"
        f"Total: {total_channels}\n"
    )
    
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

async def admin_add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add required channel"""
    query = update.callback_query
    await query.answer()
    
    context.user_data['admin_action'] = 'add_channel'
    await query.edit_message_text(
        "📢 *Add Required Channel*\n\n"
        "Please send the channel details in this format:\n\n"
        "Channel ID | Channel Name | Type\n\n"
        "Example:\n"
        "@mychannel | My Channel | public\n\n"
        "Channel ID can be @username or channel ID number.\n"
        "Type: public or private",
        parse_mode=ParseMode.MARKDOWN
    )

async def admin_handle_channel_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle channel addition"""
    if context.user_data.get('admin_action') != 'add_channel':
        return
    
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    text = update.message.text
    
    # Parse input
    parts = text.split('|')
    if len(parts) < 2:
        await update.message.reply_text(
            "❌ Invalid format. Please use:\n"
            "Channel ID | Channel Name | Type"
        )
        return
    
    channel_id = parts[0].strip()
    channel_name = parts[1].strip()
    channel_type = parts[2].strip() if len(parts) > 2 else "public"
    
    # Save to database
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO channels 
                 (channel_id, channel_name, channel_type, is_required, added_date, added_by)
                 VALUES (?,?,?,?,?,?)''',
              (channel_id, channel_name, channel_type, 1, 
               datetime.datetime.now().isoformat(), user.id))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"✅ Channel {channel_name} added successfully!")
    del context.user_data['admin_action']

async def admin_remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove required channel"""
    query = update.callback_query
    await query.answer()
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT channel_id, channel_name FROM channels WHERE is_required = 1")
    channels = c.fetchall()
    conn.close()
    
    if not channels:
        await query.edit_message_text("📭 No channels configured")
        return
    
    keyboard = []
    for channel_id, channel_name in channels:
        keyboard.append([InlineKeyboardButton(f"❌ {channel_name}", callback_data=f"delchan_{channel_id}")])
    keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="admin_back")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "Select channel to remove:",
        reply_markup=reply_markup
    )

async def admin_handle_channel_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle channel removal"""
    query = update.callback_query
    await query.answer()
    
    channel_id = query.data.split('_')[1]
    
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Channel removed successfully!")

async def admin_list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all users with stats"""
    query = update.callback_query
    await query.answer()
    
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT user_id, username, first_name, balance, completed_tasks, 
                        pending_tasks, is_verified, is_blocked, joined_date
                 FROM users ORDER BY joined_date DESC LIMIT 20''')
    users = c.fetchall()
    conn.close()
    
    if not users:
        await query.edit_message_text("📭 No users found")
        return
    
    text = "👥 *Recent Users (Last 20)*\n\n"
    for u in users:
        status = "✅" if u[6] else "❌"  # verified
        status += " 🔴" if u[7] else " 🟢"  # blocked
        text += (
            f"{status} ID: `{u[0]}`\n"
            f"Name: {u[2]} (@{u[1] or 'N/A'})\n"
            f"Balance: ₹{u[3]:.2f}\n"
            f"Tasks: {u[4]} completed, {u[5]} pending\n"
            f"Joined: {u[8][:10]}\n\n"
        )
    
    # Split long messages
    if len(text) > 4000:
        text = text[:4000] + "...\n(Truncated)"
    
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

async def admin_add_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add points to user (admin command)"""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addpoints user_id amount [reason]")
        return
    
    try:
        target_id = int(context.args[0])
        amount = float(context.args[1])
        reason = " ".join(context.args[2:]) if len(context.args) > 2 else "Admin adjustment"
    except:
        await update.message.reply_text("Invalid arguments")
        return
    
    conn = get_db()
    c = conn.cursor()
    
    # Get current balance
    c.execute("SELECT balance FROM users WHERE user_id = ?", (target_id,))
    result = c.fetchone()
    
    if not result:
        await update.message.reply_text("User not found")
        conn.close()
        return
    
    current_balance = result[0]
    new_balance = current_balance + amount
    
    # Update balance
    c.execute("UPDATE users SET balance = ?, total_earned = total_earned + ? WHERE user_id = ?",
              (new_balance, amount, target_id))
    
    # Log transaction
    log_transaction(target_id, 'credit', amount, current_balance, new_balance,
                   f"Admin add: {reason}", user.id)
    
    conn.commit()
    conn.close()
    
    # Notify user
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"💰 *Balance Updated*\n\n"
                 f"₹{amount:.2f} has been added to your balance.\n"
                 f"Reason: {reason}\n"
                 f"New Balance: ₹{new_balance:.2f}",
            parse_mode=ParseMode.MARKDOWN
        )
    except:
        pass
    
    await update.message.reply_text(f"✅ Added ₹{amount:.2f} to user {target_id}. New balance: ₹{new_balance:.2f}")

async def admin_deduct_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deduct points from user (admin command)"""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /deductpoints user_id amount [reason]")
        return
    
    try:
        target_id = int(context.args[0])
        amount = float(context.args[1])
        reason = " ".join(context.args[2:]) if len(context.args) > 2 else "Admin deduction"
    except:
        await update.message.reply_text("Invalid arguments")
        return
    
    conn = get_db()
    c = conn.cursor()
    
    # Get current balance
    c.execute("SELECT balance FROM users WHERE user_id = ?", (target_id,))
    result = c.fetchone()
    
    if not result:
        await update.message.reply_text("User not found")
        conn.close()
        return
    
    current_balance = result[0]
    
    if current_balance < amount:
        await update.message.reply_text(f"Insufficient balance. User has ₹{current_balance:.2f}")
        conn.close()
        return
    
    new_balance = current_balance - amount
    
    # Update balance
    c.execute("UPDATE users SET balance = ? WHERE user_id = ?",
              (new_balance, target_id))
    
    # Log transaction
    log_transaction(target_id, 'debit', amount, current_balance, new_balance,
                   f"Admin deduct: {reason}", user.id)
    
    conn.commit()
    conn.close()
    
    # Notify user
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"💰 *Balance Updated*\n\n"
                 f"₹{amount:.2f} has been deducted from your balance.\n"
                 f"Reason: {reason}\n"
                 f"New Balance: ₹{new_balance:.2f}",
            parse_mode=ParseMode.MARKDOWN
        )
    except:
        pass
    
    await update.message.reply_text(f"✅ Deducted ₹{amount:.2f} from user {target_id}. New balance: ₹{new_balance:.2f}")

async def admin_remove_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a task (admin command)"""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    query = update.callback_query
    if query:
        await query.answer()
        context.user_data['admin_action'] = 'remove_task'
        await query.edit_message_text(
            "Please enter the Task ID to remove:\n"
            "Use /removetask TASK_ID"
        )
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /removetask task_id")
        return
    
    try:
        task_id = int(context.args[0])
    except:
        await update.message.reply_text("Invalid task ID")
        return
    
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE tasks SET is_active = 0 WHERE task_id = ?", (task_id,))
    if c.rowcount > 0:
        conn.commit()
        await update.message.reply_text(f"✅ Task #{task_id} has been removed/deactivated.")
    else:
        await update.message.reply_text(f"❌ Task #{task_id} not found.")
    conn.close()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular messages and button presses"""
    if not update.message:
        return
    
    text = update.message.text
    
    # Check for admin actions in progress
    if context.user_data.get('admin_action') == 'add_task':
        await admin_handle_task_add(update, context)
        return
    elif context.user_data.get('admin_action') == 'add_channel':
        await admin_handle_channel_add(update, context)
        return
    
    # Handle withdrawal amount input
    if 'awaiting_withdraw_amount' in context.user_data:
        await handle_withdraw_amount(update, context)
        return
    
    # Handle withdrawal details input
    if 'awaiting_upi_details' in context.user_data or 'awaiting_gateway_details' in context.user_data:
        await handle_withdraw_details(update, context)
        return
    
    # Handle main menu buttons
    if text == "📋 Available Tasks":
        await show_tasks(update, context)
    elif text == "💰 My Balance":
        user = update.effective_user
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT balance, total_earned, total_withdrawn FROM users WHERE user_id = ?", (user.id,))
        balance, earned, withdrawn = c.fetchone()
        conn.close()
        
        await update.message.reply_text(
            f"💰 *Your Balance*\n\n"
            f"Current Balance: ₹{balance:.2f}\n"
            f"Total Earned: ₹{earned:.2f}\n"
            f"Total Withdrawn: ₹{withdrawn:.2f}",
            parse_mode=ParseMode.MARKDOWN
        )
    elif text == "📊 My Profile":
        await show_profile(update, context)
    elif text == "💳 Withdraw":
        await withdraw(update, context)
    elif text == "📞 Support":
        await update.message.reply_text(
            f"📞 *Support*\n\n"
            f"For any issues or questions, contact:\n"
            f"{SUPPORT_CONTACT}\n\n"
            f"Response time: 24-48 hours",
            parse_mode=ParseMode.MARKDOWN
        )
    elif text == "📜 History":
        user = update.effective_user
        conn = get_db()
        c = conn.cursor()
        
        # Get recent transactions
        c.execute('''SELECT type, amount, description, timestamp 
                     FROM transactions 
                     WHERE user_id = ? 
                     ORDER BY timestamp DESC LIMIT 10''', (user.id,))
        transactions = c.fetchall()
        
        # Get recent submissions
        c.execute('''SELECT s.submission_id, s.task_id, t.description, s.status, s.submitted_date
                     FROM submissions s
                     JOIN tasks t ON s.task_id = t.task_id
                     WHERE s.user_id = ?
                     ORDER BY s.submitted_date DESC LIMIT 5''', (user.id,))
        submissions = c.fetchall()
        
        # Get recent withdrawals
        c.execute('''SELECT withdrawal_id, amount, method, status, requested_date
                     FROM withdrawals
                     WHERE user_id = ?
                     ORDER BY requested_date DESC LIMIT 5''', (user.id,))
        withdrawals = c.fetchall()
        
        conn.close()
        
        history_text = "📜 *Your History*\n\n"
        
        history_text += "💸 *Recent Transactions*\n"
        if transactions:
            for t in transactions:
                emoji = "➕" if t[0] == 'credit' else "➖" if t[0] == 'debit' else "💳"
                history_text += f"{emoji} ₹{t[1]:.2f} - {t[2][:30]} ({t[3][:10]})\n"
        else:
            history_text += "No transactions yet\n"
        
        history_text += "\n📋 *Recent Submissions*\n"
        if submissions:
            for s in submissions:
                status_emoji = "✅" if s[3] == 'approved' else "❌" if s[3] == 'rejected' else "⏳"
                history_text += f"{status_emoji} Task #{s[1]}: {s[2][:20]}... ({s[4][:10]})\n"
        else:
            history_text += "No submissions yet\n"
        
        history_text += "\n💳 *Recent Withdrawals*\n"
        if withdrawals:
            for w in withdrawals:
                status_emoji = "✅" if w[3] == 'completed' else "❌" if w[3] == 'rejected' else "⏳"
                history_text += f"{status_emoji} ₹{w[1]:.2f} via {w[2]} ({w[4][:10]})\n"
        else:
            history_text += "No withdrawals yet\n"
        
        await update.message.reply_text(history_text, parse_mode=ParseMode.MARKDOWN)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    # Channel deletion
    if data.startswith('delchan_'):
        await admin_handle_channel_remove(update, context)
        return
    
    # Withdrawal method selection
    if data.startswith('withdraw_'):
        await handle_withdraw_method(update, context)
        return
    
    # Task submission
    if data.startswith('submit_'):
        await submit_task(update, context)
        return
    
    # Verification
    if data in ['verify_channels', 'verify_ip']:
        await handle_verification(update, context)
        return
    
    # Admin panel navigation
    if data == 'admin_back':
        await secret_admin_panel(update, context)
        return
    
    # Admin actions
    admin_actions = {
        'admin_add_task': admin_add_task,
        'admin_remove_task': admin_remove_task,
        'admin_list_tasks': None,  # To be implemented
        'admin_list_users': admin_list_users,
        'admin_pending_withdrawals': admin_pending_withdrawals,
        'admin_pending_submissions': admin_pending_submissions,
        'admin_financial_stats': admin_financial_stats,
        'admin_add_channel': admin_add_channel,
        'admin_remove_channel': admin_remove_channel,
        'admin_system_stats': admin_system_stats
    }
    
    if data in admin_actions and admin_actions[data]:
        await admin_actions[data](update, context)
    elif data == 'main_menu':
        await show_main_menu(update, context)

def main():
    """Main function to run the bot"""
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # User commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("tasks", show_tasks))
    application.add_handler(CommandHandler("profile", show_profile))
    application.add_handler(CommandHandler("withdraw", withdraw))
    
    # Admin commands
    application.add_handler(CommandHandler(SECRET_ADMIN_COMMAND[1:], secret_admin_panel))  # Remove / for handler
    application.add_handler(CommandHandler("addpoints", admin_add_points))
    application.add_handler(CommandHandler("deductpoints", admin_deduct_points))
    application.add_handler(CommandHandler("removetask", admin_remove_task))
    
    # Approval commands
    application.add_handler(CommandHandler("approve", admin_approve_submission))
    application.add_handler(CommandHandler("reject", admin_reject_submission))
    application.add_handler(CommandHandler("approve_withdraw", admin_approve_withdrawal))
    application.add_handler(CommandHandler("reject_withdraw", admin_reject_withdrawal))
    
    # Handle submission with task ID in command
    application.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/submit_\d+'), submit_task))
    application.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/approve_\d+'), admin_approve_submission))
    application.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/reject_\d+'), admin_reject_submission))
    application.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/approve_withdraw_\d+'), admin_approve_withdrawal))
    application.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/reject_withdraw_\d+'), admin_reject_withdrawal))
    
    # Handle photos (screenshots)
    application.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))
    
    # Handle regular messages
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Handle callbacks
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Start bot
    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()