import os
import razorpay
import json
import time
import hmac
import hashlib
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, IntegerField, TextAreaField
from wtforms.validators import InputRequired, Length, Email, EqualTo, ValidationError
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from werkzeug.utils import secure_filename


load_dotenv()

# -------------------------------
# App Configuration
# -------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///ecommerce.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False


UPLOAD_FOLDER = os.path.join('static', 'product_pics')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  


os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'

# -------------------------------
# Razorpay Client
# -------------------------------
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID', 'rzp_test_SgUOgF3qkI4iue')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET', 'rSs0S7OKhZT0E7x2up88GQCt')

razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# -------------------------------
# Database Models
# -------------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    cart_items = db.relationship('CartItem', backref='user', lazy=True)
    orders = db.relationship('Order', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False)
    price = db.Column(db.Float, nullable=False)
    stock = db.Column(db.Integer, default=0)
    category = db.Column(db.String(50), nullable=False)
    image_url = db.Column(db.String(200), default='https://via.placeholder.com/300')
    image_filename = db.Column(db.String(200), nullable=True)
    cart_items = db.relationship('CartItem', backref='product', lazy=True)
    order_items = db.relationship('OrderItem', backref='product', lazy=True)

class CartItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity = db.Column(db.Integer, default=1)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    order_date = db.Column(db.DateTime, default=datetime.utcnow)
    total_amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='Pending')
    razorpay_order_id = db.Column(db.String(100), nullable=True)
    razorpay_payment_id = db.Column(db.String(100), nullable=True)
    address = db.Column(db.Text, nullable=True)
    items = db.relationship('OrderItem', backref='order', lazy=True)

class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)

# -------------------------------
# Forms
# -------------------------------
class RegistrationForm(FlaskForm):
    username = StringField('Username', validators=[InputRequired(), Length(min=2, max=20)])
    email = StringField('Email', validators=[InputRequired(), Email()])
    password = PasswordField('Password', validators=[InputRequired(), Length(min=6)])
    confirm_password = PasswordField('Confirm Password', validators=[InputRequired(), EqualTo('password')])
    submit = SubmitField('Sign Up')

class LoginForm(FlaskForm):
    email = StringField('Email', validators=[InputRequired(), Email()])
    password = PasswordField('Password', validators=[InputRequired()])
    submit = SubmitField('Login')

class AddToCartForm(FlaskForm):
    quantity = IntegerField('Quantity', validators=[InputRequired()], default=1)
    submit = SubmitField('Add to Cart')

class CheckoutForm(FlaskForm):
    address = TextAreaField('Shipping Address', validators=[InputRequired(), Length(min=10)])
    submit = SubmitField('Place Order')
    
def get_cart_total():
    if current_user.is_authenticated:
        cart_items = CartItem.query.filter_by(user_id=current_user.id).all()
        total = sum(item.product.price * item.quantity for item in cart_items)
        return total
    return 0.0

def get_cart_count():
    if current_user.is_authenticated:
        return CartItem.query.filter_by(user_id=current_user.id).count()
    return 0

def get_product_image(product):
    """Get the correct image URL for a product"""
    if product.image_filename:
        return url_for('static', filename='product_pics/' + product.image_filename)
    elif product.image_url:
        return product.image_url
    else:
        return 'https://via.placeholder.com/300'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.context_processor
def inject_globals():
    return dict(cart_count=get_cart_count())

# -------------------------------
# Template Filter for INR
# -------------------------------
@app.template_filter('inr')
def format_inr(value):
    """Indian Rupee format"""
    try:
        value = float(value)
        if value.is_integer():
            value = int(value)
        return f'₹{value:,.2f}' if isinstance(value, float) and not value.is_integer() else f'₹{value:,}'
    except:
        return f'₹{value}'

@app.template_filter('product_image')
def product_image_filter(product):
    """Template filter to get product image"""
    return get_product_image(product)


@app.route('/')
def index():
    categories = {
        'mens': "Men's",
        'womens': "Women's",
        'kids': "Kids ",
        'mobiles': "Mobiles",
        'electronics': "Electronics",
        'home_appliances': "Home Appliances",
        'toys': "Toys"
    }
    category_products = {}
    for cat_key in categories.keys():
        category_products[cat_key] = Product.query.filter_by(category=cat_key).all()
    return render_template('index.html', categories=categories, category_products=category_products)


@app.route('/search')
def search():
    query = request.args.get('q', '')
    if query:
        products = Product.query.filter(
            Product.name.contains(query) | Product.description.contains(query)
        ).all()
    else:
        products = Product.query.all()
    
    categories = {
        'mens': "Men's",
        'womens': "Women's",
        'kids': "Kids "
        ""
        "",
        'mobiles': "Mobiles",
        'electronics': "Electronics",
        'home_appliances': "Home Appliances",
        'toys': "Toys"
    }
    category_products = {}
    for cat_key in categories.keys():
        category_products[cat_key] = Product.query.filter_by(category=cat_key).all()
    
    return render_template('index.html', categories=categories, category_products=category_products, search_query=query)

@app.route('/category/<string:category_name>')
def category_view(category_name):
    products = Product.query.filter_by(category=category_name).all()
    return render_template('category.html', category=category_name, products=products)

@app.route('/product/<int:product_id>')
def product_detail(product_id):
    product = Product.query.get_or_404(product_id)
    form = AddToCartForm()
    return render_template('product_detail.html', product=product, form=form)

@app.route('/add-to-cart/<int:product_id>')
@login_required
def add_to_cart(product_id):
    product = Product.query.get_or_404(product_id)
    
    
    if product.stock <= 0:
        flash(f'Sorry, {product.name} is out of stock!', 'danger')
        return redirect(url_for('product_detail', product_id=product_id))
    
    
    cart_item = CartItem.query.filter_by(user_id=current_user.id, product_id=product_id).first()
    
    if cart_item:
        
        if cart_item.quantity + 1 > product.stock:
            flash(f'Sorry, only {product.stock} items available!', 'danger')
            return redirect(url_for('product_detail', product_id=product_id))
        cart_item.quantity += 1
    else:
        
        cart_item = CartItem(user_id=current_user.id, product_id=product_id, quantity=1)
        db.session.add(cart_item)
    
    db.session.commit()
    
    
    check = CartItem.query.filter_by(user_id=current_user.id, product_id=product_id).first()
    if check:
        flash(f'✓ Added {product.name} to cart!', 'success')
    else:
        flash(f'✗ Failed to add {product.name} to cart.', 'danger')
    
    return redirect(url_for('cart'))

@app.route('/cart')
@login_required
def cart():
    cart_items = CartItem.query.filter_by(user_id=current_user.id).all()
    total = 0
    for item in cart_items:
        total += item.product.price * item.quantity
    
    print(f"Cart items for user {current_user.id}: {len(cart_items)}")  # Debug print
    return render_template('cart.html', cart_items=cart_items, total=total)

@app.route('/update-cart/<int:item_id>', methods=['POST'])
@login_required
def update_cart(item_id):
    cart_item = CartItem.query.get_or_404(item_id)
    if cart_item.user_id != current_user.id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('cart'))
    new_quantity = int(request.form.get('quantity', 1))
    if new_quantity <= 0:
        db.session.delete(cart_item)
        flash('Item removed', 'info')
    elif new_quantity > cart_item.product.stock:
        flash(f'Sorry, only {cart_item.product.stock} items available!', 'danger')
    else:
        cart_item.quantity = new_quantity
        flash('Cart updated', 'success')
    db.session.commit()
    return redirect(url_for('cart'))

@app.route('/remove-from-cart/<int:item_id>')
@login_required
def remove_from_cart(item_id):
    cart_item = CartItem.query.get_or_404(item_id)
    if cart_item.user_id != current_user.id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('cart'))
    db.session.delete(cart_item)
    db.session.commit()
    flash('Item removed from cart!', 'info')
    return redirect(url_for('cart'))

@app.route('/checkout', methods=['GET', 'POST'])
@login_required
def checkout():
    cart_items = CartItem.query.filter_by(user_id=current_user.id).all()
    if not cart_items:
        flash('Your cart is empty.', 'warning')
        return redirect(url_for('cart'))
    
    total = get_cart_total()
    
    if request.method == 'GET':
        form = CheckoutForm()
        return render_template('checkout.html', cart_items=cart_items, total=total, form=form, razorpay_key_id=RAZORPAY_KEY_ID)
    
    form = CheckoutForm()
    if form.validate_on_submit():
        session['shipping_address'] = form.address.data
        return render_template('payment.html', cart_items=cart_items, total=total, razorpay_key_id=RAZORPAY_KEY_ID)
    
    return render_template('checkout.html', cart_items=cart_items, total=total, form=form, razorpay_key_id=RAZORPAY_KEY_ID)

# -------------------------------
# Razorpay Payment Endpoints
# -------------------------------
@app.route('/create-order', methods=['POST'])
@login_required
def create_order():
    cart_items = CartItem.query.filter_by(user_id=current_user.id).all()
    if not cart_items:
        return jsonify({'error': 'Cart is empty'}), 400

    total_rupees = get_cart_total()
    amount_paise = int(total_rupees * 100)

    if amount_paise < 100:
        return jsonify({'error': 'Minimum order amount is ₹1'}), 400

    try:
        order = razorpay_client.order.create({
            'amount': amount_paise,
            'currency': 'INR',
            'receipt': f'receipt_{int(time.time())}',
            'payment_capture': 1
        })
        session['razorpay_order_id'] = order['id']
        return jsonify({
            'id': order['id'],
            'amount': order['amount'],
            'currency': order['currency']
        })
    except Exception as e:
        app.logger.error(f"Razorpay order creation failed: {str(e)}")
        return jsonify({'error': 'Could not create payment order'}), 500

@app.route('/verify-payment', methods=['POST'])
@login_required
def verify_payment():
    data = request.get_json()
    razorpay_order_id = data.get('razorpay_order_id')
    razorpay_payment_id = data.get('razorpay_payment_id')
    razorpay_signature = data.get('razorpay_signature')

    if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature]):
        return jsonify({'status': 'failure', 'error': 'Missing payment details'}), 400

    try:
        razorpay_client.utility.verify_payment_signature({
            'razorpay_order_id': razorpay_order_id,
            'razorpay_payment_id': razorpay_payment_id,
            'razorpay_signature': razorpay_signature
        })
    except razorpay.errors.SignatureVerificationError:
        return jsonify({'status': 'failure', 'error': 'Signature verification failed'}), 400

    cart_items = CartItem.query.filter_by(user_id=current_user.id).all()
    if not cart_items:
        return jsonify({'status': 'failure', 'error': 'Cart is empty'}), 400

    total_amount = get_cart_total()
    address = session.get('shipping_address', 'No address provided')
    
    new_order = Order(
        user_id=current_user.id,
        total_amount=total_amount,
        status='Paid',
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_id=razorpay_payment_id,
        address=address
    )
    db.session.add(new_order)
    db.session.flush()

    for item in cart_items:
        order_item = OrderItem(
            order_id=new_order.id,
            product_id=item.product_id,
            quantity=item.quantity,
            price=item.product.price
        )
        db.session.add(order_item)
        product = item.product
        product.stock -= item.quantity
        db.session.delete(item)

    db.session.commit()
    session.pop('razorpay_order_id', None)
    session.pop('shipping_address', None)

    return jsonify({'status': 'success', 'order_id': new_order.id})

# -------------------------------
# Order Management
# -------------------------------
@app.route('/order_confirmation')
@app.route('/order_confirmation/<int:order_id>')
def order_confirmation(order_id=None):
    if order_id is None:
        # Get latest order or handle appropriately
        order = Order.query.filter_by(user_id=current_user.id).first()
    else:
        order = Order.query.get_or_404(order_id)
    return render_template('order_confirmation.html', order=order)

@app.route('/orders')
@login_required
def orders():
    user_orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.order_date.desc()).all()
    return render_template('orders.html', orders=user_orders)

# -------------------------------
# Authentication Routes
# -------------------------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    form = RegistrationForm()
    if form.validate_on_submit():
        user = User(username=form.username.data, email=form.email.data)
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        flash('Account created! Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html', form=form)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            flash('Logged in successfully!', 'success')
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('index'))
        else:
            flash('Login unsuccessful. Check email and password.', 'danger')
    return render_template('login.html', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))

# -------------------------------
# tables and sample products with images
# -------------------------------
with app.app_context():
    db.create_all()
    if Product.query.count() == 0:
        print("Adding sample products with images...")
        
        products = []
        
        # ========== MEN'S ==========
        mens_products = [
            ('Classic White Shirt', 1299, 'Premium cotton, slim fit shirt for men', 'mens', 50, 'white_shirt.jpg'),
            ('Blue Denim Jeans', 2499, 'Stretchable denim jeans, regular fit', 'mens', 50, 'denim_jeans.jpg'),
            ('Black Blazer', 4999, 'Formal blazer for parties and meetings', 'mens', 50, 'blazer.jpg'),
            ('Casual Hoodie', 1899, 'Cotton blend hoodie for winter', 'mens', 50, 'hoodie.jpg'),
            ('Leather Jacket', 5999, 'Genuine leather jacket', 'mens', 50, 'leather_jacket.jpg'),
            ('Formal Shoes', 3499, 'Leather office shoes', 'mens', 50, 'formal_shoes.jpg'),
            ('Wrist Watch', 1999, 'Analog stainless steel watch', 'mens', 50, 'watch.jpg'),
            ('Navy Blue Shirt', 1499, 'Cotton navy blue formal shirt', 'mens', 50, 'navy_shirt.jpg'),
            ('Grey Sweatpants', 1299, 'Comfortable cotton sweatpants', 'mens', 50, 'sweatpants.jpg'),
            ('Brown Loafers', 3999, 'Premium leather loafers', 'mens', 50, 'loafers.jpg'),
            ('Sports Cap', 499, 'Adjustable sports cap', 'mens', 50, 'cap.jpg'),
            ('Running Shoes', 4499, 'Lightweight running shoes', 'mens', 50, 'running_shoes.jpg'),
            ('Leather Wallet', 899, 'Premium leather wallet', 'mens', 50, 'wallet.jpg'),
            ('Belt', 599, 'Genuine leather belt', 'mens', 50, 'belt.jpg'),
            ('Socks Pack', 399, '6 pair cotton socks', 'mens', 50, 'socks.jpg'),
            ('Track Pants', 1199, 'Comfortable track pants', 'mens', 50, 'track_pants.jpg'),
            ('Polo T-Shirt', 999, 'Classic polo t-shirt', 'mens', 50, 'polo_tshirt.jpg'),
            ('Winter Gloves', 699, 'Warm winter gloves', 'mens', 50, 'gloves.jpg'),
            ('Beanie', 499, 'Winter woolen beanie', 'mens', 50, 'beanie.jpg'),
            ('Formal Vest', 1899, 'Elegant formal vest', 'mens', 50, 'vest.jpg'),
            ('Neck Tie', 499, 'Silk neck tie', 'mens', 50, 'neck_tie.jpg'),
            ('Perfume', 1299, 'Premium men perfume', 'mens', 50, 'perfume.jpg'),
            ('Shaving Kit', 1299, 'Complete shaving kit', 'mens', 50, 'shaving_kit.jpg'),
            ('Sunglasses', 1299, 'UV protection sunglasses', 'mens', 50, 'sunglasses.jpg'),
            ('Gym Bag', 1499, 'Spacious gym bag', 'mens', 50, 'gym_bag.jpg'),
            ('Tie Set', 599, '5 piece formal tie set', 'mens', 50, 'ties.jpg'),
            ('Cufflinks Set', 799, 'Premium cufflinks set', 'mens', 50, 'cufflinks.jpg'),
            ('Deodorant', 349, 'Long lasting deodorant', 'mens', 50, 'deodorant.jpg'),
            ('Face Wash', 299, 'Men face wash', 'mens', 50, 'face_wash.jpg'),
            ('Moisturizer', 399, 'Men moisturizer cream', 'mens', 50, 'moisturizer.jpg'),
            ('Beard Oil', 599, 'Natural beard oil', 'mens', 50, 'beard_oil.jpg'),
            ('Razor Set', 899, 'Premium razor set', 'mens', 50, 'razor.jpg'),
            ('Shoe Polish Kit', 399, 'Complete shoe polish kit', 'mens', 50, 'shoe_polish.jpg'),
            
        ]
        
        for name, price, desc, cat, stock, img in mens_products:
            products.append(Product(
                name=name, price=price, description=desc,
                image_url='https://via.placeholder.com/300',
                image_filename=img, category=cat, stock=stock
            ))
        
        # ========== WOMEN'S  ==========
        womens_products = [
            ('Floral Maxi Dress', 2499, 'Beautiful summer floral print dress', 'womens', 50, 'floral_dress.jpg'),
            ('Designer Saree', 3999, 'Banarasi silk saree with embroidery', 'womens', 50, 'saree.jpg'),
            ('Handbag', 2999, 'Leather tote bag', 'womens', 50, 'handbag.jpg'),
            ('High Heel Sandals', 2499, 'Party wear heels', 'womens', 50, 'heels.jpg'),
            ('Women Watch', 1799, 'Elegant rose gold watch', 'womens', 50, 'women_watch.jpg'),
            ('Kurti Set', 1899, 'Cotton kurti with dupatta', 'womens', 50, 'kurti.jpg'),
            ('Leggings', 799, 'Stretchable cotton leggings', 'womens', 50, 'leggings.jpg'),
            ('Women Jeans', 1999, 'Slim fit denim jeans', 'womens', 50, 'women_jeans.jpg'),
            ('Top', 899, 'Casual cotton top', 'womens', 50, 'top.jpg'),
            ('Skirt', 1299, 'A-line floral skirt', 'womens', 50, 'skirt.jpg'),
            ('Blazer Women', 4499, 'Formal women blazer', 'womens', 50, 'women_blazer.jpg'),
            ('Jumpsuit', 2999, 'Trendy jumpsuit', 'womens', 50, 'jumpsuit.jpg'),
            ('Shrug', 1499, 'Lightweight shrug', 'womens', 50, 'shrug.jpg'),
            ('Crop Top', 899, 'Stylish crop top', 'womens', 50, 'crop_top.jpg'),
            ('Palazzo Pants', 1299, 'Comfortable palazzo', 'womens', 50, 'palazzo.jpg'),
            ('Jewellery Set', 1599, 'Necklace and earrings set', 'womens', 50, 'jewellery.jpg'),
            ('Bangles', 499, 'Glass bangles set', 'womens', 50, 'bangles.jpg'),
            ('Earrings', 399, 'Fashion earrings', 'womens', 50, 'earrings.jpg'),
            ('Necklace', 899, 'Designer necklace', 'womens', 50, 'necklace.jpg'),
            ('Ring Set', 599, '5 piece ring set', 'womens', 50, 'rings.jpg'),
            ('Clutch Bag', 1199, 'Evening clutch', 'womens', 50, 'clutch.jpg'),
            ('Backpack Women', 1899, 'Fashion backpack', 'womens', 50, 'women_backpack.jpg'),
            ('Makeup Kit', 1499, 'Complete makeup kit', 'womens', 50, 'makeup_kit.jpg'),
            ('Lipstick Set', 899, '5 shade lipstick set', 'womens', 50, 'lipstick.jpg'),
            ('Nail Polish Set', 599, '6 color nail polish', 'womens', 50, 'nail_polish.jpg'),
            ('Foundation', 799, 'Liquid foundation', 'womens', 50, 'foundation.jpg'),
            ('Kajal', 299, 'Long lasting kajal', 'womens', 50, 'kajal.jpg'),
            ('Mascara', 499, 'Volume mascara', 'womens', 50, 'mascara.jpg'),
            ('Eyeliner', 349, 'Waterproof eyeliner', 'womens', 50, 'eyeliner.jpg'),
            ('Compact Powder', 599, 'Mattifying compact', 'womens', 50, 'compact.jpg'),
            ('Perfume Women', 999, 'Floral perfume', 'womens', 50, 'women_perfume.jpg'),
            ('Hair Dryer', 1299, 'Professional hair dryer', 'womens', 50, 'hair_dryer.jpg'),
            ('Straightener', 999, 'Hair straightener', 'womens', 50, 'straightener.jpg'),
            ('Curling Iron', 1499, 'Hair curling iron', 'womens', 50, 'curling_iron.jpg'),
            ('Hair Brush', 399, 'Detangling brush', 'womens', 50, 'hair_brush.jpg'),
            ('Sling Bag', 999, 'Casual sling bag', 'womens', 50, 'sling_bag.jpg'),
            ('Tote Bag', 1499, 'Large tote bag', 'womens', 50, 'tote_bag.jpg'),
            ('Sunglasses Women', 1299, 'Fashion sunglasses', 'womens', 50, 'women_sunglasses.jpg'),
            ('Bracelet', 399, 'Silver bracelet', 'womens', 50, 'bracelet.jpg'),
            ('Anklet', 299, 'Designer anklet', 'womens', 50, 'anklet.jpg'),
            ('Hair Clip Set', 199, '8 piece hair clip', 'womens', 50, 'hair_clip.jpg'),
            ('Tummy Trimmer', 999, 'Waist trimmer belt', 'womens', 50, 'tummy_trimmer.jpg'),
            ('Lehenga', 5999, 'Designer wedding lehenga', 'womens', 50, 'lehenga.jpg'),
            ('Gown', 4499, 'Evening gown', 'womens', 50, 'gown.jpg'),
            ('Hair Band Set', 299, '5 piece hair band', 'womens', 50, 'hair_band.jpg'),
            ('Scrunchie Set', 199, '6 piece scrunchie', 'womens', 50, 'scrunchie.jpg'),
        ]
        
        for name, price, desc, cat, stock, img in womens_products:
            products.append(Product(
                name=name, price=price, description=desc,
                image_url='https://via.placeholder.com/300',
                image_filename=img, category=cat, stock=stock
            ))
        
        # ========== KIDS ==========
        kids_products = [
            ('Kids T-Shirt', 399, 'Cotton cartoon print t-shirt', 'kids', 50, 'kids_tshirt.jpg'),
            ('Kids Jeans', 799, 'Stretchable denim jeans', 'kids', 50, 'kids_jeans.jpg'),
            ('Kids Sneakers', 899, 'Lightweight sports shoes', 'kids', 50, 'kids_sneakers.jpg'),
            ('Kids Frocks', 899, 'Princess design frock', 'kids', 50, 'kids_frock.jpg'),
            ('School Uniform Shirt', 499, 'White cotton shirt', 'kids', 50, 'school_shirt.jpg'),
            ('School Pants', 599, 'Navy blue trousers', 'kids', 50, 'school_pants.jpg'),
            ('Kids Hoodie', 999, 'Warm winter hoodie', 'kids', 50, 'kids_hoodie.jpg'),
            ('Kids Sandals', 599, 'Soft sole, colorful', 'kids', 50, 'kids_sandals.jpg'),
            ('Kids Backpack', 699, 'Cartoon character bag', 'kids', 50, 'kids_backpack.jpg'),
            ('Water Bottle', 299, 'Leak proof, BPA free', 'kids', 50, 'water_bottle.jpg'),
            ('Lunch Box', 399, 'Insulated, 3 compartments', 'kids', 50, 'lunch_box.jpg'),
            ('Pencil Box', 199, 'Zip case with stationery', 'kids', 50, 'pencil_box.jpg'),
            ('Kids Watch', 799, 'Colorful, water resistant', 'kids', 50, 'kids_watch.jpg'),
            ('Cap for Kids', 299, 'Cotton, adjustable', 'kids', 50, 'kids_cap.jpg'),
            ('Kids Jacket', 1299, 'Warm winter jacket', 'kids', 50, 'kids_jacket.jpg'),
            ('Sweater for Kids', 899, 'Soft wool blend', 'kids', 50, 'kids_sweater.jpg'),
            ('Pajama Set', 599, 'Cotton, comfortable sleepwear', 'kids', 50, 'pajama.jpg'),
            ('Swimsuit', 699, 'Quick dry, UV protection', 'kids', 50, 'swimsuit.jpg'),
            ('Raincoat', 499, 'Waterproof with hood', 'kids', 50, 'raincoat.jpg'),
            ('Kids Sunglasses', 299, 'UV protection, flexible', 'kids', 50, 'kids_sunglasses.jpg'),
            ('Toy Car', 499, 'Pull back, friction car', 'kids', 50, 'toy_car.jpg'),
            ('Building Blocks', 399, '100 pieces set', 'kids', 50, 'building_blocks.jpg'),
            ('Doll', 599, 'Soft toy, 12 inches', 'kids', 50, 'doll.jpg'),
            ('Drawing Book', 199, '50 pages, spiral bound', 'kids', 50, 'drawing_book.jpg'),
            ('Crayons Set', 149, '12 colors, non-toxic', 'kids', 50, 'crayons.jpg'),
            ('Story Book', 299, 'Illustrated story book', 'kids', 50, 'story_book.jpg'),
            ('Puzzle', 399, '100 piece puzzle', 'kids', 50, 'puzzle.jpg'),
            ('Action Figure', 799, 'Superhero action figure', 'kids', 50, 'action_figure.jpg'),
            ('Toy Train', 1499, 'Battery operated train', 'kids', 50, 'toy_train.jpg'),
            ('Play Doh', 599, '10 color play doh set', 'kids', 50, 'play_doh.jpg'),
            ('Bicycle', 4999, '16 inch bicycle', 'kids', 50, 'bicycle.jpg'),
            ('Scooter', 2499, 'Foldable scooter', 'kids', 50, 'scooter.jpg'),
            ('Kite', 199, 'Diamond shape kite', 'kids', 50, 'kite.jpg'),
            ('Slime Kit', 399, '6 color slime kit', 'kids', 50, 'slime_kit.jpg'),
            ('Art Set', 699, '40 piece art set', 'kids', 50, 'art_set.jpg'),
            ('Musical Toy', 499, 'Piano, drum set', 'kids', 50, 'musical_toy.jpg'),
            ('Magic Set', 499, '50 magic tricks', 'kids', 50, 'magic_set.jpg'),
            ('Science Kit', 1299, 'Chemistry lab set', 'kids', 50, 'science_kit.jpg'),
            ('Dinosaur Set', 699, '6 piece dinosaur set', 'kids', 50, 'dinosaur.jpg'),
            ('Doctor Set', 599, '15 piece doctor kit', 'kids', 50, 'doctor_set.jpg'),
            ('Kitchen Set', 899, 'Pretend kitchen set', 'kids', 50, 'kitchen_set.jpg'),
            ('Educational Laptop', 999, '20 learning activities', 'kids', 50, 'edu_laptop.jpg'),
            ('Ball', 299, 'Soft rubber ball', 'kids', 50, 'ball.jpg'),
            ('Bubble Gun', 399, 'Automatic bubble gun', 'kids', 50, 'bubble_gun.jpg'),
            ('Remote Control Car', 1299, 'Rechargeable RC car', 'kids', 50, 'rc_car.jpg'),
        ]
        
        for name, price, desc, cat, stock, img in kids_products:
            products.append(Product(
                name=name, price=price, description=desc,
                image_url='https://via.placeholder.com/300',
                image_filename=img, category=cat, stock=stock
            ))
        
        # ========== MOBILES ==========
        mobiles_products = [
            ('iPhone 15 Pro', 129999, 'A17 chip, 256GB storage', 'mobiles', 25, 'm.jpg'),
            ('Samsung Galaxy S24', 99999, 'AI features, 256GB', 'mobiles', 25, 'samsung_s24.jpg'),
            ('Google Pixel 8', 79999, 'Best camera phone', 'mobiles', 25, 'pixel8.jpg'),
            ('OnePlus 12', 64999, 'Snapdragon 8 Gen 3', 'mobiles', 25, 'oneplus12.jpg'),
            ('Xiaomi 14 Ultra', 89999, 'Leica camera, 512GB', 'mobiles', 25, 'xiaomi.jpg'),
            ('Nothing Phone 2', 44999, 'Glyph interface', 'mobiles', 25, 'nothing_phone.jpg'),
            ('Realme GT 5G', 39999, 'Gaming phone', 'mobiles', 25, 'realme_gt.jpg'),
            ('Vivo X100 Pro', 89999, 'Zeiss camera', 'mobiles', 25, 'vivo_x100.jpg'),
            ('Oppo Find N3', 139999, 'Foldable phone', 'mobiles', 25, 'oppo_find.jpg'),
            ('Motorola Edge 50', 37999, 'Curved display', 'mobiles', 25, 'motorola_edge.jpg'),
            ('iQOO Neo 9', 36999, 'Dimensity 9300', 'mobiles', 25, 'iqoo_neo.jpg'),
            ('Poco F6', 29999, 'Flagship killer', 'mobiles', 25, 'poco_f6.jpg'),
            ('Samsung A55', 39999, 'Mid-range bestseller', 'mobiles', 25, 'samsung_a55.jpg'),
            ('Redmi Note 13 Pro', 25999, '200MP camera', 'mobiles', 25, 'redmi_note.jpg'),
            ('Infinix GT 20', 19999, 'Budget gaming', 'mobiles', 25, 'infinix_gt.jpg'),
            ('Tecno Camon 30', 18999, '64MP camera', 'mobiles', 25, 'tecno_camon.jpg'),
            ('Lava Agni 2', 23999, 'Made in India', 'mobiles', 25, 'lava_agni.jpg'),
            ('Nokia G42', 15999, 'Durable, clean Android', 'mobiles', 25, 'nokia_g42.jpg'),
            ('Micromax IN 2B', 9999, 'Budget phone', 'mobiles', 25, 'micromax_in.jpg'),
            ('JioPhone 5G', 3999, 'Basic 5G phone', 'mobiles', 25, 'jiophone.jpg'),
            ('iPhone 14', 69999, 'A15 chip, 128GB', 'mobiles', 25, 'iphone14.jpg'),
            ('Samsung Flip 5', 99999, 'Foldable flip phone', 'mobiles', 25, 'samsung_flip.jpg'),
            ('Pixel 7a', 43999, 'Google Tensor G2', 'mobiles', 25, 'pixel7a.jpg'),
            ('OnePlus Nord CE 4', 29999, 'Mid-range, 100W', 'mobiles', 25, 'oneplus_nord.jpg'),
            ('Realme 12 Pro+', 29999, 'Periscope camera', 'mobiles', 25, 'realme12.jpg'),
            ('Vivo V30', 34999, '5G smartphone', 'mobiles', 25, 'vivo_v30.jpg'),
            ('Oppo Reno', 39999, 'Camera phone', 'mobiles', 25, 'oppo_reno.jpg'),
            ('Poco X6 Pro', 26999, 'Dimensity 8300', 'mobiles', 25, 'poco_x6.jpg'),
            ('Samsung M54', 34999, '6000mAh battery', 'mobiles', 25, 'samsung_m54.jpg'),
            ('Google Pixel Fold', 139999, 'Foldable Pixel', 'mobiles', 25, 'pixel_fold.jpg'),
            ('iPhone SE', 49999, 'Compact iPhone', 'mobiles', 25, 'iphone_se.jpg'),
            ('Nothing Phone 1', 34999, 'Glyph lights', 'mobiles', 25, 'nothing_phone1.jpg'),
            ('Asus ROG 8', 89999, 'Gaming phone', 'mobiles', 25, 'asus_rog.jpg'),
            ('Sony Xperia', 79999, '4K display', 'mobiles', 25, 'sony_xperia.jpg'),
            ('LG Wing', 59999, 'Swivel screen', 'mobiles', 25, 'lg_wing.jpg'),
            ('HTC U23', 39999, 'Mid-range', 'mobiles', 25, 'htc_u23.jpg'),
            ('Honor Magic', 69999, 'Magic OS', 'mobiles', 25, 'honor_magic.jpg'),
            ('Black Shark 6', 54999, 'Gaming phone', 'mobiles', 25, 'black_shark.jpg'),
            ('ZTE Axon', 44999, 'Under display camera', 'mobiles', 25, 'zte_axon.jpg'),
            ('Razer Phone 3', 69999, '120Hz gaming', 'mobiles', 25, 'razer_phone.jpg'),
            ('Fairphone 5', 59999, 'Sustainable', 'mobiles', 25, 'fairphone.jpg'),
            ('Cat S75', 49999, 'Rugged phone', 'mobiles', 25, 'cat_s75.jpg'),
            ('Ulefone Armor', 39999, 'Waterproof', 'mobiles', 25, 'ulefone.jpg'),
            ('Doogee V30', 49999, 'Thermal camera', 'mobiles', 25, 'doogee.jpg'),
            ('Oukitel WP39', 29999, 'Rugged budget', 'mobiles', 25, 'oukitel.jpg'),
        ]
        
        for name, price, desc, cat, stock, img in mobiles_products:
            products.append(Product(
                name=name, price=price, description=desc,
                image_url='https://via.placeholder.com/300',
                image_filename=img, category=cat, stock=stock
            ))
        
        # ========== ELECTRONICS ==========
        electronics_products = [
            ('Noise Cancelling Headphones', 3999, 'Premium sound quality', 'electronics', 25, 'headphones.jpg'),
            ('Bluetooth Speaker', 2499, 'Portable speaker', 'electronics', 25, 'speaker.jpg'),
            ('Smart Watch', 2999, 'Fitness tracker', 'electronics', 25, 'smartwatch.jpg'),
            ('Wireless Mouse', 799, 'Ergonomic mouse', 'electronics', 25, 'mouse.jpg'),
            ('Mechanical Keyboard', 3499, 'RGB backlit', 'electronics', 25, 'keyboard.jpg'),
            ('Power Bank', 1999, '20000mAh fast charging', 'electronics', 25, 'powerbank.jpg'),
            ('Laptop Stand', 1299, 'Aluminum stand', 'electronics', 25, 'laptop_stand.jpg'),
            ('USB Hub', 599, '4 port USB 3.0', 'electronics', 25, 'usb_hub.jpg'),
            ('Webcam HD', 1999, '1080p webcam', 'electronics', 25, 'webcam.jpg'),
            ('Gaming Controller', 2499, 'Bluetooth controller', 'electronics', 25, 'controller.jpg'),
            ('Ring Light', 1499, '10 inches', 'electronics', 25, 'ring_light.jpg'),
            ('Microphone', 2999, 'Cardioid mic', 'electronics', 25, 'microphone.jpg'),
            ('Smart Plug', 999, 'WiFi voice control', 'electronics', 25, 'smart_plug.jpg'),
            ('LED Strip', 899, '5 meters RGB', 'electronics', 25, 'led_strip.jpg'),
            ('Router', 2999, 'AC1200 dual band', 'electronics', 25, 'router.jpg'),
            ('SSD 1TB', 6499, 'SATA III', 'electronics', 25, 'ssd.jpg'),
            ('External HDD', 5999, '2TB USB 3.0', 'electronics', 25, 'external_hdd.jpg'),
            ('Pen Drive', 499, '64GB USB 3.2', 'electronics', 25, 'pendrive.jpg'),
            ('Charging Cable', 299, 'Type-C 3 pack', 'electronics', 25, 'cable.jpg'),
            ('Car Charger', 399, '2 ports fast charging', 'electronics', 25, 'car_charger.jpg'),
            ('Tripod', 1299, 'Adjustable height', 'electronics', 25, 'tripod.jpg'),
            ('Selfie Stick', 499, 'Bluetooth remote', 'electronics', 25, 'selfie_stick.jpg'),
            ('VR Headset', 1499, 'Foldable phone compatible', 'electronics', 25, 'vr_headset.jpg'),
            ('Action Camera', 7999, '4K waterproof', 'electronics', 25, 'action_camera.jpg'),
            ('Mini Drone', 12999, 'Foldable camera drone', 'electronics', 25, 'drone.jpg'),
            ('Tablet', 19999, '10 inch Android tablet', 'electronics', 25, 'tablet.jpg'),
            ('Earbuds', 2999, 'Wireless earbuds', 'electronics', 25, 'earbuds.jpg'),
            ('Smart Band', 1999, 'Fitness tracker', 'electronics', 25, 'smart_band.jpg'),
            ('Digital Watch', 1499, 'Digital display watch', 'electronics', 25, 'digital_watch.jpg'),
            ('Calculator', 499, 'Scientific calculator', 'electronics', 25, 'calculator.jpg'),
            ('Digital Scale', 899, 'Kitchen digital scale', 'electronics', 25, 'digital_scale.jpg'),
            ('Room Thermometer', 399, 'Digital thermometer', 'electronics', 25, 'thermometer.jpg'),
            ('Night Lamp', 699, 'Smart night lamp', 'electronics', 25, 'night_lamp.jpg'),
            ('Phone Stand', 299, 'Adjustable phone stand', 'electronics', 25, 'phone_stand.jpg'),
            ('Cable Organizer', 199, '5 piece set', 'electronics', 25, 'cable_organizer.jpg'),
            ('Screen Protector', 299, 'Tempered glass', 'electronics', 25, 'screen_protector.jpg'),
            ('Phone Case', 399, 'Shockproof case', 'electronics', 25, 'phone_case.jpg'),
            ('Camera Lens Kit', 1999, 'Phone camera lens set', 'electronics', 25, 'lens_kit.jpg'),
            ('Gimbal Stabilizer', 8999, '3 axis phone gimbal', 'electronics', 25, 'gimbal.jpg'),
            ('Voice Recorder', 2999, 'Digital voice recorder', 'electronics', 25, 'voice_recorder.jpg'),
            ('FM Radio', 999, 'Portable FM radio', 'electronics', 25, 'fm_radio.jpg'),
            ('Clock Radio', 1499, 'Digital clock radio', 'electronics', 25, 'clock_radio.jpg'),
            ('Weather Station', 3999, 'Digital weather station', 'electronics', 25, 'weather_station.jpg'),
            ('USB Fan', 399, 'Mini USB fan', 'electronics', 25, 'usb_fan.jpg'),
            ('Desk Lamp', 1299, 'LED desk lamp', 'electronics', 25, 'desk_lamp.jpg'),
        ]
        
        for name, price, desc, cat, stock, img in electronics_products:
            products.append(Product(
                name=name, price=price, description=desc,
                image_url='https://via.placeholder.com/300',
                image_filename=img, category=cat, stock=stock
            ))
        
        # ========== HOME APPLIANCES ==========
        home_products = [
            ('Mixer Grinder', 3999, '500W, 3 jars', 'home_appliances', 20, 'mixer.jpg'),
            ('Air Fryer', 4999, '4.5L oil-free', 'home_appliances', 20, 'airfryer.jpg'),
            ('Electric Kettle', 1299, '1.5L stainless steel', 'home_appliances', 20, 'kettle.jpg'),
            ('Induction Cooktop', 2999, '2100W touch control', 'home_appliances', 20, 'induction.jpg'),
            ('Rice Cooker', 2499, '1.8L non-stick', 'home_appliances', 20, 'rice_cooker.jpg'),
            ('Toaster', 1499, '2 slots adjustable', 'home_appliances', 20, 'toaster.jpg'),
            ('Sandwich Maker', 999, 'Non-stick plates', 'home_appliances', 20, 'sandwich_maker.jpg'),
            ('Juicer', 3599, '500W centrifugal', 'home_appliances', 20, 'juicer.jpg'),
            ('Microwave Oven', 12999, '20L convection', 'home_appliances', 20, 'microwave.jpg'),
            ('Refrigerator Mini', 14999, '50L tabletop', 'home_appliances', 20, 'refrigerator.jpg'),
            ('Washing Machine', 24999, '6.5kg semi-auto', 'home_appliances', 20, 'washing_machine.jpg'),
            ('Vacuum Cleaner', 3999, '600W bagless', 'home_appliances', 20, 'vacuum_cleaner.jpg'),
            ('Iron Box', 999, 'Non-stick steam', 'home_appliances', 20, 'iron.jpg'),
            ('Hair Dryer', 1299, '1600W ionic', 'home_appliances', 20, 'hair_dryer.jpg'),
            ('Room Heater', 1999, '1500W fan heater', 'home_appliances', 20, 'room_heater.jpg'),
            ('Ceiling Fan', 2499, '1200mm energy efficient', 'home_appliances', 20, 'ceiling_fan.jpg'),
            ('Table Fan', 1499, '400mm oscillating', 'home_appliances', 20, 'table_fan.jpg'),
            ('Air Cooler', 7999, '20L desert cooler', 'home_appliances', 20, 'air_cooler.jpg'),
            ('Water Purifier', 9999, 'RO+UV 8L', 'home_appliances', 20, 'water_purifier.jpg'),
            ('Sewing Machine', 8999, 'Manual with stand', 'home_appliances', 20, 'sewing_machine.jpg'),
            ('Food Processor', 6499, '1000W 8 attachments', 'home_appliances', 20, 'food_processor.jpg'),
            ('Vegetable Chopper', 699, 'Manual 4 blades', 'home_appliances', 20, 'chopper.jpg'),
            ('Hot Pot', 999, '1.5L non-stick', 'home_appliances', 20, 'hot_pot.jpg'),
            ('Electric Pan', 1799, 'Non-stick deep', 'home_appliances', 20, 'electric_pan.jpg'),
            ('Dishwasher', 19999, 'Tabletop 4 settings', 'home_appliances', 20, 'dishwasher.jpg'),
            ('Coffee Maker', 3999, 'Drip coffee maker', 'home_appliances', 20, 'coffee_maker.jpg'),
            ('Egg Boiler', 599, '7 egg capacity', 'home_appliances', 20, 'egg_boiler.jpg'),
            ('Steamer', 1499, '3 tier steamer', 'home_appliances', 20, 'steamer.jpg'),
            ('Slow Cooker', 2499, '3L slow cooker', 'home_appliances', 20, 'slow_cooker.jpg'),
            ('Pressure Cooker', 1999, '5L electric', 'home_appliances', 20, 'pressure_cooker.jpg'),
            ('Popcorn Maker', 1299, 'Hot air popcorn', 'home_appliances', 20, 'popcorn_maker.jpg'),
            ('Ice Cream Maker', 2999, '1.5L capacity', 'home_appliances', 20, 'ice_cream_maker.jpg'),
            ('Yogurt Maker', 1499, '7 jar set', 'home_appliances', 20, 'yogurt_maker.jpg'),
            ('Bread Maker', 5999, 'Automatic bread machine', 'home_appliances', 20, 'bread_maker.jpg'),
            ('Pizza Maker', 3999, 'Electric pizza oven', 'home_appliances', 20, 'pizza_maker.jpg'),
            ('Waffle Maker', 1999, 'Non-stick plates', 'home_appliances', 20, 'waffle_maker.jpg'),
            ('Crepe Maker', 1499, 'Non-stick surface', 'home_appliances', 20, 'crepe_maker.jpg'),
            ('Electric Grill', 2999, 'Indoor grill', 'home_appliances', 20, 'grill.jpg'),
            ('Fryer', 3999, 'Deep fryer', 'home_appliances', 20, 'fryer.jpg'),
            ('Charger', 299, 'Multi USB charger', 'home_appliances', 20, 'charger.jpg'),
            ('Extension Cord', 399, '6 socket with USB', 'home_appliances', 20, 'extension.jpg'),
            ('Power Strip', 499, 'Surge protector', 'home_appliances', 20, 'power_strip.jpg'),
            ('Timer Switch', 299, 'Digital timer', 'home_appliances', 20, 'timer_switch.jpg'),
            ('Smart Bulb', 599, 'WiFi RGB bulb', 'home_appliances', 20, 'smart_bulb.jpg'),
            ('Smart Socket', 799, 'WiFi smart plug', 'home_appliances', 20, 'smart_socket.jpg'),
        ]
        
        for name, price, desc, cat, stock, img in home_products:
            products.append(Product(
                name=name, price=price, description=desc,
                image_url='https://via.placeholder.com/300',
                image_filename=img, category=cat, stock=stock
            ))
        
        # ========== TOYS==========
        toys_products = [
            ('Lego Building Blocks', 1499, '250 pieces creative set', 'toys', 30, 'lego.jpg'),
            ('Remote Control Car', 1299, 'Rechargeable RC car', 'toys', 30, 'rc_car.jpg'),
            ('Stuffed Teddy Bear', 599, '12 inches soft toy', 'toys', 30, 'teddy.jpg'),
            ('Doll House', 1999, '3 floors with furniture', 'toys', 30, 'doll_house.jpg'),
            ('Board Game Chess', 399, 'Magnetic chess', 'toys', 30, 'chess.jpg'),
            ('Puzzle', 499, '500 pieces', 'toys', 30, 'puzzle.jpg'),
            ('Action Figure', 799, 'Marvel superhero', 'toys', 30, 'action_figure.jpg'),
            ('Toy Train', 1499, 'Battery operated', 'toys', 30, 'toy_train.jpg'),
            ('Play Doh', 599, '10 colors with molds', 'toys', 30, 'play_doh.jpg'),
            ('Bicycle', 4999, '16 inches training wheels', 'toys', 30, 'bicycle.jpg'),
            ('Scooter', 2499, 'Foldable LED wheels', 'toys', 30, 'scooter.jpg'),
            ('Kite', 199, 'Diamond shape with string', 'toys', 30, 'kite.jpg'),
            ('Slime Kit', 399, '6 colors with glitter', 'toys', 30, 'slime.jpg'),
            ('Art Set', 699, '40 pieces watercolor', 'toys', 30, 'art_set.jpg'),
            ('Musical Toy', 499, 'Piano drum xylophone', 'toys', 30, 'musical_toy.jpg'),
            ('Building Blocks', 999, '50 large blocks', 'toys', 30, 'jumbo_blocks.jpg'),
            ('Toy Gun', 299, 'Soft dart target set', 'toys', 30, 'toy_gun.jpg'),
            ('Rubik Cube', 199, '3x3 speed cube', 'toys', 30, 'rubik.jpg'),
            ('Magic Set', 499, '50 tricks instructions', 'toys', 30, 'magic_set.jpg'),
            ('Science Kit', 1299, 'Chemistry experiments', 'toys', 30, 'science_kit.jpg'),
            ('Dinosaur Set', 699, '6 plastic dinosaurs', 'toys', 30, 'dinosaur.jpg'),
            ('Car Parking Toy', 999, 'Wooden ramp 4 cars', 'toys', 30, 'parking_toy.jpg'),
            ('Doctor Set', 599, 'Pretend play 15 pieces', 'toys', 30, 'doctor_set.jpg'),
            ('Kitchen Set', 899, 'Utensils stove food', 'toys', 30, 'kitchen_set.jpg'),
            ('Educational Laptop', 999, '20 learning activities', 'toys', 30, 'edu_laptop.jpg'),
            ('Balloon Set', 299, '50 colorful balloons', 'toys', 30, 'balloons.jpg'),
            ('Flying Disc', 199, 'Soft frisbee', 'toys', 30, 'frisbee.jpg'),
            ('Yo Yo', 99, 'Light up yo yo', 'toys', 30, 'yoyo.jpg'),
            ('Jump Rope', 149, 'Adjustable skipping rope', 'toys', 30, 'jump_rope.jpg'),
            ('Hula Hoop', 299, 'Colorful hula hoop', 'toys', 30, 'hula_hoop.jpg'),
            ('Bubble Machine', 799, 'Automatic bubble maker', 'toys', 30, 'bubble_machine.jpg'),
            ('Water Gun', 399, 'Super soaker', 'toys', 30, 'water_gun.jpg'),
            ('Sand Set', 499, 'Beach sand toys', 'toys', 30, 'sand_set.jpg'),
            ('Pool Toy', 599, 'Inflatable pool toys', 'toys', 30, 'pool_toy.jpg'),
            ('Tent', 1299, 'Play tent for kids', 'toys', 30, 'tent.jpg'),
            ('Marble Run', 799, 'DIY marble track', 'toys', 30, 'marble_run.jpg'),
            ('Magnetic Tiles', 1499, '24 piece magnetic set', 'toys', 30, 'magnetic_tiles.jpg'),
            ('Sticker Book', 299, '1000+ stickers', 'toys', 30, 'sticker_book.jpg'),
            ('Coloring Book', 199, '50 pages coloring', 'toys', 30, 'coloring_book.jpg'),
            ('Water Color', 149, '12 color water paint', 'toys', 30, 'water_color.jpg'),
            ('Clay Set', 399, '8 color modeling clay', 'toys', 30, 'clay.jpg'),
            ('Origami Paper', 199, '100 sheets colorful', 'toys', 30, 'origami.jpg'),
            ('Spinning Top', 99, 'Traditional spinning top', 'toys', 30, 'spinning_top.jpg'),
            ('Kendama', 299, 'Skill toy', 'toys', 30, 'kendama.jpg'),
            ('Juggling Balls', 399, '3 piece juggling set', 'toys', 30, 'juggling_balls.jpg'),
        ]
        
        for name, price, desc, cat, stock, img in toys_products:
            products.append(Product(
                name=name, price=price, description=desc,
                image_url='https://via.placeholder.com/300',
                image_filename=img, category=cat, stock=stock
            ))
        
        db.session.add_all(products)
        db.session.commit()
        print(f"✅ Added {len(products)} products with image support!")
        
        print("\n" + "="*60)
        print("📸 IMAGE SETUP INSTRUCTIONS:")
        print("="*60)
        print("Create folder: static/product_pics/")
        print("Add these image files to static/product_pics/:")
        print("="*60)
        # -------------------------------
# Simple Admin Page (change product prices)
# -------------------------------
@app.route('/admin')
@login_required
def admin_products():
    # Only allow users with is_admin=True (you can set yourself as admin)
    if not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('index'))
    
    products = Product.query.order_by(Product.category, Product.id).all()
    return render_template('admin_products.html', products=products)

@app.route('/admin/update-price/<int:product_id>', methods=['POST'])
@login_required
def admin_update_price(product_id):
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    product = Product.query.get_or_404(product_id)
    new_price = request.form.get('price')
    try:
        product.price = float(new_price)
        db.session.commit()
        flash(f'Price of {product.name} updated to ₹{product.price}', 'success')
    except ValueError:
        flash('Invalid price', 'danger')
    
    return redirect(url_for('admin_products'))
@app.route('/secret-admin/<secret>')
def secret_admin(secret):
    if secret != 'sparkcart123':          # pick any secret word
        return "Page not found", 404
    products = Product.query.all()
    return render_template('admin_products.html', products=products)
@app.route('/secret-admin/update/<int:product_id>', methods=['POST'])
def secret_admin_update(product_id):
    # optional: same secret check (or rely on referrer)
    # For simplicity, we skip secret check here (only accessible via POST)
    product = Product.query.get_or_404(product_id)
    try:
        new_price = float(request.form['price'])
        product.price = new_price
        db.session.commit()
        flash(f'Price of {product.name} updated to ₹{new_price}', 'success')
    except:
        flash('Invalid price', 'danger')
    return redirect(url_for('secret_admin', secret='sparkcart123'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))