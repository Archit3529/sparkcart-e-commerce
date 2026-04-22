from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User, Product, Order, OrderItem
from forms import RegistrationForm, LoginForm, CheckoutForm
import razorpay
import json
import hmac
import hashlib
from datetime import datetime

from dotenv import load_dotenv
import os

load_dotenv()

RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET')

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///ecommerce.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# ---------- Template filter for Indian Rupee ----------
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

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ---------- Helper: get cart from session ----------
def get_cart():
    cart = session.get('cart', {})
    return cart

def save_cart(cart):
    session['cart'] = cart

# ---------- Routes ----------
@app.route('/')
def index():
    all_products = Product.query.all()
    categories = {
        'mens': 'Men\'s ',
        'womens': 'Women\'s',
        'kids': 'Kids',
        'mobiles': 'Mobiles',
        'electronics': 'Electronics',
        'home_appliances': 'Home Appliances',
        'toys': 'Toys'
    }
    category_products = {}
    for cat_key in categories.keys():
        category_products[cat_key] = Product.query.filter_by(category=cat_key).all()
    return render_template('index.html', 
                           categories=categories, 
                           category_products=category_products,
                           razorpay_key=RAZORPAY_KEY_ID)   

@app.route('/category/<string:category_name>')
def category_view(category_name):
    products = Product.query.filter_by(category=category_name).all()
    return render_template('category.html', category=category_name, products=products)

@app.route('/product/<int:id>')
def product_detail(id):
    product = Product.query.get_or_404(id)
    return render_template('product_detail.html', product=product)

@app.route('/add-to-cart/<int:product_id>')
def add_to_cart(product_id):
    cart = get_cart()
    cart[str(product_id)] = cart.get(str(product_id), 0) + 1
    save_cart(cart)
    flash('Product added to cart!', 'success')
    return redirect(url_for('index'))

@app.route('/cart')
def view_cart():
    cart = get_cart()
    cart_items = []
    total = 0
    for product_id, quantity in cart.items():
        product = Product.query.get(int(product_id))
        if product:
            item_total = product.price * quantity
            total += item_total
            cart_items.append({
                'product': product,
                'quantity': quantity,
                'total': item_total
            })
    return render_template('cart.html', cart_items=cart_items, total=total)

@app.route('/remove-from-cart/<int:product_id>')
def remove_from_cart(product_id):
    cart = get_cart()
    if str(product_id) in cart:
        del cart[str(product_id)]
        save_cart(cart)
        flash('Item removed from cart.', 'info')
    return redirect(url_for('view_cart'))

@app.route('/update-cart/<int:product_id>/<int:quantity>')
def update_cart(product_id, quantity):
    cart = get_cart()
    if quantity <= 0:
        cart.pop(str(product_id), None)
    else:
        cart[str(product_id)] = quantity
    save_cart(cart)
    return redirect(url_for('view_cart'))

# ---------- Checkout & Payment ----------
@app.route('/checkout', methods=['GET', 'POST'])
@login_required
def checkout():
    # Get current cart from session
    cart = get_cart()
    if not cart:
        flash('Your cart is empty.', 'warning')
        return redirect(url_for('index'))
    
    # Calculate total amount
    total = 0
    for product_id, quantity in cart.items():
        product = Product.query.get(int(product_id))
        if product:
            total += product.price * quantity
    
    # ----- GET request: show address form -----
    if request.method == 'GET':
        form = CheckoutForm()
        return render_template('checkout.html', form=form, total=total)
    
    # ----- POST request: address submitted, create order & Razorpay payment -----
    form = CheckoutForm()
    if form.validate_on_submit():
        # Create order object (not yet committed)
        order = Order(total=total, user_id=current_user.id)
        db.session.add(order)
        db.session.flush()   # get order.id without committing
        
        # Create order items
        for product_id, quantity in cart.items():
            product = Product.query.get(int(product_id))
            if product:
                order_item = OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    quantity=quantity,
                    price=product.price
                )
                db.session.add(order_item)
        
        # Create Razorpay order
        amount_in_paise = int(total * 100)
        order_data = {
            'amount': amount_in_paise,
            'currency': 'INR',
            'receipt': f'order_{order.id}',
            'payment_capture': 1
        }
        
        try:
            razorpay_order = client.order.create(data=order_data)
        except Exception as e:
            # Rollback the entire database transaction – nothing saved
            db.session.rollback()
            flash('Payment gateway error. Please try again.', 'danger')
            return redirect(url_for('view_cart'))
        
        # If we reach here, Razorpay order succeeded – commit the database
        db.session.commit()
        
        # Store IDs in session for later verification
        session['razorpay_order_id'] = razorpay_order['id']
        session['pending_order_id'] = order.id
        
        # Show payment page
        return render_template(
            'payment.html',
            razorpay_key=RAZORPAY_KEY_ID,
            razorpay_order=razorpay_order,
            total=total,
            order=order
        )
    else:
        return render_template('checkout.html', form=form, total=total)

# ---------- Payment Success Handler (Step 6) ----------
@app.route('/payment-success', methods=['POST'])
@login_required
def payment_success():
    data = request.get_json()
    
    # Verify payment signature
    params_dict = {
        'razorpay_order_id': data['razorpay_order_id'],
        'razorpay_payment_id': data['razorpay_payment_id'],
        'razorpay_signature': data['razorpay_signature']
    }
    
    try:
        client.utility.verify_payment_signature(params_dict)
        # Payment is verified
        order_id = session.get('pending_order_id')
        if order_id:
            # Optionally update order status (if you add a 'status' column)
            # order = Order.query.get(order_id)
            # order.status = 'paid'
            # db.session.commit()
            
            # Clear the cart and session data
            session.pop('cart', None)
            session.pop('razorpay_order_id', None)
            session.pop('pending_order_id', None)
            
            return jsonify({'success': True, 'order_id': order_id})
        else:
            return jsonify({'success': False, 'error': 'No pending order'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/order/<int:order_id>')
@login_required
def order_confirmation(order_id):
    order = Order.query.get_or_404(order_id)
    if order.user_id != current_user.id:
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    return render_template('order_confirmation.html', order=order)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    form = RegistrationForm()
    if form.validate_on_submit():
        hashed_pw = generate_password_hash(form.password.data)
        user = User(username=form.username.data, email=form.email.data, password=hashed_pw)
        db.session.add(user)
        db.session.commit()
        flash('Registration successful. Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html', form=form)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and check_password_hash(user.password, form.password.data):
            login_user(user)
            flash('Logged in successfully!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid email or password.', 'danger')
    return render_template('login.html', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))

# ---------- Create tables and add sample products ----------
with app.app_context():
    db.create_all()
    if Product.query.count() == 0:
        categories = {
            'mens': 'Men\'s',
            'womens': 'Women\'s',
            'kids': 'Kids',
            'mobiles': 'Mobiles',
            'electronics': 'Electronics',
            'home_appliances': 'Home Appliances',
            'toys': 'Toys'
        }
        products = []
        for cat_key, cat_name in categories.items():
            if cat_key == 'mens':
                base_names = ['Cotton T-Shirt', 'Denim Jeans', 'Casual Shirt', 'Hoodie', 'Jacket', 'Sweater', 'Trousers', 'Shorts', 'Blazer', 'Polo Shirt', 'Vest', 'Track Pants', 'Formal Shirt', 'Chinos', 'Socks Pack', 'Belt', 'Cap', 'Sunglasses', 'Watch', 'Sneakers']
                base_price = 999
            elif cat_key == 'womens':
                base_names = ['Floral Dress', 'Handbag', 'High Heels', 'Scarf', 'Blouse', 'Skirt', 'Leggings', 'Jacket', 'Saree', 'Kurti', 'Jewellery Set', 'Sunglasses', 'Watch', 'Clutch', 'Makeup Kit', 'Perfume', 'Tops', 'Jeans', 'Sandals', 'Bangles']
                base_price = 1299
            elif cat_key == 'kids':
                base_names = ['T-Shirt', 'Shorts', 'Frock', 'Toy Car', 'Pajamas', 'Sweater', 'School Bag', 'Water Bottle', 'Lunch Box', 'Sneakers', 'Cap', 'Socks Pack', 'Story Book', 'Crayons Set', 'Puzzle', 'Doll', 'Action Figure', 'Backpack', 'Raincoat', 'Pencil Box']
                base_price = 499
            elif cat_key == 'mobiles':
                base_names = ['Smartphone 5G', 'Budget Phone', 'Gaming Phone', 'Foldable Phone', 'Rugged Phone', 'Phablet', 'Basic Phone', 'Dual SIM Phone', 'Camera Phone', 'Battery King', 'Compact Phone', 'Waterproof Phone', 'Student Phone', 'Senior Phone', 'Refurbished Phone', 'Flagship Phone', 'Midrange Phone', 'Selfie Phone', 'Business Phone', 'E-waste Recycled Phone']
                base_price = 14999
            elif cat_key == 'electronics':
                base_names = ['Bluetooth Speaker', 'Noise Cancelling Headphones', 'Smart Watch', 'Laptop Stand', 'USB Hub', 'Power Bank', 'Webcam', 'Gaming Mouse', 'Mechanical Keyboard', 'Monitor', 'SSD Drive', 'External HDD', 'Router', 'Smart Plug', 'LED Strip', 'Phone Case', 'Screen Protector', 'Charging Cable', 'Car Charger', 'Drone']
                base_price = 1999
            elif cat_key == 'home_appliances':
                base_names = ['Mixer Grinder', 'Air Fryer', 'Induction Cooktop', 'Rice Cooker', 'Toaster', 'Kettle', 'Vacuum Cleaner', 'Iron Box', 'Hair Dryer', 'Room Heater', 'Ceiling Fan', 'Table Fan', 'Air Cooler', 'Water Purifier', 'Sewing Machine', 'Microwave Oven', 'Refrigerator (Mini)', 'Washing Machine (Portable)', 'Dishwasher (Tabletop)', 'Food Processor']
                base_price = 3999
            elif cat_key == 'toys':
                base_names = ['Lego Set', 'Remote Control Car', 'Doll House', 'Stuffed Animal', 'Board Game', 'Puzzle 500pc', 'Action Figure', 'Toy Train', 'Play Doh Set', 'Bicycle', 'Scooter', 'Kite', 'Slime Kit', 'Art Set', 'Musical Toy', 'Building Blocks', 'Toy Gun', 'Rubik Cube', 'Magic Set', 'Science Kit']
                base_price = 799
            else:
                continue
            
            for i, name in enumerate(base_names[:20]):
                variation = (i % 5) * 50 - (i % 3) * 30
                price = base_price + variation
                price = max(price, 50)
                products.append(Product(
                    name=f'{name} - {cat_name}',
                    price=round(price, 2),
                    description=f'High-quality {cat_name.lower()} product. Perfect for everyday use.',
                    image_url='https://via.placeholder.com/200',
                    category=cat_key
                ))
        db.session.add_all(products)
        db.session.commit()
        print(f"Added {len(products)} products with INR prices.")

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)