from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed, FileRequired
from wtforms import StringField, PasswordField, BooleanField, SubmitField, DecimalField, SelectField
from wtforms.validators import DataRequired, Email, Length, EqualTo, NumberRange


class SignupForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired(), Length(max=150)])
    email = StringField('Email', validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=8, message='Password must be at least 8 characters.')])
    confirm_password = PasswordField(
        'Confirm password',
        validators=[DataRequired(), EqualTo('password', message='Passwords must match.')]
    )
    submit = SubmitField('Create account')


class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember = BooleanField('Remember me')
    submit = SubmitField('Log in')


class DemoLoadForm(FlaskForm):
    persona = SelectField('Persona', choices=[
        ('student', 'Student on a stipend'),
        ('salaried', 'Salaried professional'),
        ('freelancer', 'Freelancer with irregular income'),
    ])
    submit = SubmitField('Load demo data')


class UploadForm(FlaskForm):
    file = FileField('Bank statement CSV', validators=[
        FileRequired(message='Please choose a CSV file.'),
        FileAllowed(['csv'], 'CSV files only.')
    ])
    submit = SubmitField('Upload')


class BudgetForm(FlaskForm):
    category_id = SelectField('Category', validators=[DataRequired()])
    monthly_limit = DecimalField('Monthly limit', validators=[DataRequired(), NumberRange(min=0)])
    submit = SubmitField('Save budget')


class GoalForm(FlaskForm):
    name = StringField('Goal name', validators=[DataRequired(), Length(max=150)])
    target_amount = DecimalField('Target amount', validators=[DataRequired(), NumberRange(min=1)])
    deadline = StringField('Deadline (YYYY-MM-DD)', validators=[DataRequired()])
    submit = SubmitField('Add goal')


class SettingsForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired(), Length(max=150)])
    currency = SelectField('Currency', choices=[('INR', 'INR (₹)'), ('USD', 'USD ($)'), ('EUR', 'EUR (€)')])
    alert_sensitivity = DecimalField(
        'Alert sensitivity (0-1, lower = more alerts)',
        validators=[DataRequired(), NumberRange(min=0, max=1)]
    )
    submit = SubmitField('Save settings')
