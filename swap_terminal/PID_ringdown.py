import numpy as np
import matplotlib.pyplot as plt
import tensorflow as tf
from tensorflow.keras import layers, models
from scipy.optimize import curve_fit

# Set seeds for reproducibility
np.random.seed(42)
tf.random.set_seed(42)

# ===============================
# System Parameters
# ===============================
dt = 0.01                          # time step (seconds)
t_data = np.arange(0, 10, dt)        # time vector

# True system parameters (thermal dynamics)
tau_R = np.random.uniform(1.0, 2.0)      # room/time constant [s]
tau_h = np.random.uniform(0.5, 1.0)      # heater/air stream time constant [s]
T_amb = 20.0                           # ambient temperature in °C
k1 = np.random.uniform(0.05, 0.15)       # coupling gain
k2 = np.random.uniform(0.01, 0.05)       # nonlinear heat loss/gain factor
k3 = np.random.uniform(0.5, 1.5)         # gain from control signal to heater effect
m = np.random.uniform(0.8, 1.2)          # effective mass flow rate

# Initial conditions
T0 = T_amb + np.random.uniform(5, 10)    # initial tool temperature
T_h0 = T_amb + np.random.uniform(2, 5)     # initial heater temperature

# ===============================
# Runge-Kutta 4th Order Integration
# ===============================
def thermal_dynamics(state, u):
    """
    Computes the derivatives for the thermal system.
    state: [T, T_h]
    u: control signal
    """
    T, T_h = state
    dT_dt   = - (T - T_amb)/tau_R + k1 * m * (T_h - T) + k2 * np.sin(T - T_amb)
    dT_h_dt = - (T_h - T_amb)/tau_h + k3 * u
    return np.array([dT_dt, dT_h_dt])

def rk4_step(state, u, dt):
    """
    Performs one Runge-Kutta 4 integration step.
    """
    k1_vec = thermal_dynamics(state, u)
    k2_vec = thermal_dynamics(state + dt/2 * k1_vec, u)
    k3_vec = thermal_dynamics(state + dt/2 * k2_vec, u)
    k4_vec = thermal_dynamics(state + dt * k3_vec, u)
    return state + dt/6 * (k1_vec + 2*k2_vec + 2*k3_vec + k4_vec)

# ===============================
# Step 1: Generate Synthetic Data (Open-Loop, using RK4)
# ===============================
T_data = np.zeros_like(t_data)
T_h_data = np.zeros_like(t_data)
T_data[0] = T0
T_h_data[0] = T_h0

u_step = 1.0  # constant control input for open-loop simulation
state = np.array([T0, T_h0])
for i in range(1, len(t_data)):
    state = rk4_step(state, u_step, dt)
    T_data[i] = state[0]
    T_h_data[i] = state[1]

# Compute steady state and normalization factor
steady_state = np.mean(T_data[-int(0.2*len(T_data)):])
delta = steady_state - T_amb
if abs(delta) < 1e-6:
    delta = 1e-6

y_true = (T_data - T_amb) / delta  # normalized response
noise = np.random.normal(0, 0.03, size=y_true.shape)
y_noisy = y_true + noise

plt.figure(figsize=(8, 4))
plt.plot(t_data, y_noisy, 'b.', label='Synthetic Data (Noisy)')
plt.plot(t_data, y_true, 'k--', label='True Normalized Response')
plt.xlabel("Time (s)")
plt.ylabel("Normalized Temperature Response")
plt.title("Synthetic Nonlinear Step Response Data (RK4 Integration)")
plt.legend()
plt.grid(True)
plt.show()

# -------------------------------
# Step 1.5: Estimate Natural Frequency and Damping Ratio
# -------------------------------
def second_order_step(t, wn, zeta):
    """
    Second-order step response for an underdamped system.
    Assumes unit final value.
    """
    phi = np.arccos(zeta)
    return 1 - (1/np.sqrt(1 - zeta**2)) * np.exp(-zeta * wn * t) * np.sin(wn * np.sqrt(1 - zeta**2) * t + phi)

# Use a subset of data (e.g., first 5 seconds) for fitting
fit_mask = t_data <= 5.0
t_fit = t_data[fit_mask]
y_fit = y_true[fit_mask]

# Initial guess: ωₙ=2, ζ=0.5; bounds: ωₙ>0 and 0<ζ<1
p0 = [2.0, 0.5]
bounds = ([0, 0], [np.inf, 1])
params, _ = curve_fit(second_order_step, t_fit, y_fit, p0=p0, bounds=bounds)
wn_est, zeta_est = params
print(f"Estimated natural frequency (ωₙ): {wn_est:.4f} rad/s, Damping ratio (ζ): {zeta_est:.4f}")

# ===============================
# Step 2: Generate Training Data via Improved PID Control (using RK4)
# ===============================
# PID settings (improved design) now based on estimated ωₙ
safety_factor = 0.5
K_p = safety_factor * (2 * wn_est - 1)
K_i = safety_factor * (wn_est**2)
K_d = safety_factor * (0.1 * wn_est)

# Desired absolute setpoint (°C) and corresponding normalized setpoint
T_set = 250.0  # desired setpoint in °C
T_set_norm = (T_set - T_amb) / delta  # normalized setpoint
print("Desired normalized setpoint:", T_set_norm)

T_cl = np.zeros_like(t_data)
T_h_cl = np.zeros_like(t_data)
T_cl[0] = T0
T_h_cl[0] = T_h0

integral = 0.0
prev_error = T_set_norm - ((T0 - T_amb) / delta)
u_array = np.zeros_like(t_data)
deriv_filtered = 0.0
tau_D = 0.05
alpha = dt / (tau_D + dt)
integral_limit = 1.0

X_train = []
y_train = []

# Reinitialize state for closed-loop PID simulation
state = np.array([T0, T_h0])
for i in range(1, len(t_data)):
    y_current = (state[0] - T_amb) / delta
    error = T_set_norm - y_current
    integral += error * dt
    integral = np.clip(integral, -integral_limit, integral_limit)
    deriv_raw = (error - prev_error) / dt
    deriv_filtered = (1 - alpha) * deriv_filtered + alpha * deriv_raw
    
    # PID control law
    u = K_p * error + K_i * integral + K_d * deriv_filtered
    u_array[i] = u
    
    # Record training sample: [error, derivative] -> control signal u
    X_train.append([error, deriv_filtered])
    y_train.append(u)
    
    state = rk4_step(state, u, dt)
    T_cl[i] = state[0]
    T_h_cl[i] = state[1]
    
    prev_error = error

X_train = np.array(X_train)
y_train = np.array(y_train)

plt.figure(figsize=(8, 4))
y_cl_norm = (T_cl - T_amb) / delta
plt.plot(t_data, y_cl_norm, label="Normalized Room Temperature (PID)")
plt.axhline(T_set_norm, color='r', linestyle='--', label="Setpoint (Normalized)")
plt.xlabel("Time (s)")
plt.ylabel("Normalized Temperature")
plt.title("Closed-Loop Response with Classical PID Control (RK4 Integration)")
plt.legend()
plt.grid(True)
plt.show()

# ===============================
# Step 3: Hyperparameter Tuning for the Neural Network Controller
# ===============================
def build_model(neurons):
    model = models.Sequential([
        layers.Dense(neurons, activation='relu', input_shape=(2,)),
        layers.Dense(neurons, activation='relu'),
        layers.Dense(1)
    ])
    model.compile(optimizer='adam', loss='mse')
    return model

# Define a small grid of hyperparameters: varying number of neurons and training epochs
hyperparams = [
    {"neurons": 16, "epochs": 100},
    {"neurons": 32, "epochs": 200}
]

best_model = None
best_loss = np.inf
history_dict = {}

for hp in hyperparams:
    model_temp = build_model(hp['neurons'])
    history_temp = model_temp.fit(X_train, y_train, epochs=hp['epochs'], batch_size=32, verbose=0)
    final_loss = history_temp.history['loss'][-1]
    label = f"neurons={hp['neurons']}, epochs={hp['epochs']}"
    history_dict[label] = history_temp.history['loss']
    print(f"{label}: Final Training Loss = {final_loss:.4f}")
    if final_loss < best_loss:
         best_loss = final_loss
         best_model = model_temp

plt.figure(figsize=(6,4))
for label, loss in history_dict.items():
    plt.plot(loss, label=label)
plt.xlabel('Epoch')
plt.ylabel('MSE Loss')
plt.title('Hyperparameter Tuning: Training Loss Curves')
plt.legend()
plt.grid(True)
plt.show()

# ===============================
# Step 4: Simulate Closed-Loop Control Using the Neural Network Controller (RK4 Integration)
# ===============================
T_nn = np.zeros_like(t_data)
T_h_nn = np.zeros_like(t_data)
T_nn[0] = T0
T_h_nn[0] = T_h0

# Reinitialize state for NN-based closed-loop simulation
state = np.array([T0, T_h0])
prev_error_nn = T_set_norm - ((T0 - T_amb) / delta)
u_nn_array = np.zeros_like(t_data)
deriv_filtered_nn = 0.0
integral_nn = 0.0

for i in range(1, len(t_data)):
    y_current_nn = (state[0] - T_amb) / delta
    error_nn = T_set_norm - y_current_nn
    integral_nn += error_nn * dt
    integral_nn = np.clip(integral_nn, -integral_limit, integral_limit)
    deriv_raw_nn = (error_nn - prev_error_nn) / dt
    deriv_filtered_nn = (1 - alpha) * deriv_filtered_nn + alpha * deriv_raw_nn

    # Use the best NN controller to predict control signal
    nn_input = np.array([[error_nn, deriv_filtered_nn]])
    u_nn = best_model.predict(nn_input, verbose=0)[0,0]
    u_nn_array[i] = u_nn

    state = rk4_step(state, u_nn, dt)
    T_nn[i] = state[0]
    T_h_nn[i] = state[1]

    prev_error_nn = error_nn

plt.figure(figsize=(10, 8))
plt.subplot(2, 1, 1)
y_nn_norm = (T_nn - T_amb) / delta
plt.plot(t_data, y_nn_norm, label="Normalized Room Temperature (NN)")
plt.axhline(T_set_norm, color='r', linestyle='--', label="Setpoint (Normalized)")
plt.xlabel("Time (s)")
plt.ylabel("Normalized Temperature")
plt.title("Closed-Loop Response with NN Controller (RK4 Integration)")
plt.legend()
plt.grid(True)

plt.subplot(2, 1, 2)
plt.plot(t_data, u_nn_array, label="Control Input u(t) [NN]")
plt.xlabel("Time (s)")
plt.ylabel("Control Signal")
plt.title("Neural Network Control Signal")
plt.legend()
plt.grid(True)

plt.tight_layout()
plt.show()
