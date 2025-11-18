import json
import numpy as np
from typing import Dict, List, Any, Optional
import logging
import os
import pickle
import platform

# File locking support (Unix/Linux only, Windows uses different mechanism)
IS_WINDOWS = platform.system() == "Windows"
if not IS_WINDOWS:
    import fcntl
else:
    # Windows doesn't have fcntl, use msvcrt for file locking
    try:
        import msvcrt
    except ImportError:
        msvcrt = None

logger = logging.getLogger(__name__)

# Path to system metrics configuration file
SYSTEM_METRICS_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), 
    "system_metrics_config.json"
)


class ModelPersistence:
    """
    Mixin class for persisting and loading model state with file locking
    to prevent race conditions between multiple processes.
    """
    
    def __init__(self, *args, **kwargs):
        """Initialize persistence with model state file path."""
        super().__init__(*args, **kwargs)
        self.model_state_file = None
        self.lock_file = None
        # In-memory cache for performance
        self._cached_state = None
        self._cache_mtime = 0
        self._cache_ttl = 1.0  # Cache for 1 second to reduce file I/O
    
    def set_persistence_path(self, state_file_path: str):
        """
        Set the path for model state persistence.
        
        Args:
            state_file_path: Path to the file where model state will be saved
        """
        self.model_state_file = state_file_path
        self.lock_file = state_file_path + ".lock"
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(state_file_path) if os.path.dirname(state_file_path) else '.', exist_ok=True)
    
    def save_state(self):
        """Save current model state to file with file locking."""
        if not self.model_state_file:
            return
        
        try:
            state = self._get_state()
            
            if IS_WINDOWS and msvcrt:
                # Windows file locking
                with open(self.lock_file, 'w') as lock_f:
                    try:
                        msvcrt.locking(lock_f.fileno(), msvcrt.LK_LOCK, 1)
                        
                        # Save model state
                        with open(self.model_state_file, 'wb') as f:
                            pickle.dump(state, f)
                        
                        # Update cache (deep copy for numpy arrays)
                        if isinstance(state, dict):
                            import copy
                            self._cached_state = copy.deepcopy(state)
                        else:
                            self._cached_state = state
                        self._cache_mtime = os.path.getmtime(self.model_state_file)
                        
                        logger.debug(f"Saved model state to {self.model_state_file}")
                    finally:
                        try:
                            msvcrt.locking(lock_f.fileno(), msvcrt.LK_UNLCK, 1)
                        except:
                            pass
            else:
                # Unix/Linux file locking
                with open(self.lock_file, 'w') as lock_f:
                    try:
                        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
                        
                        # Save model state
                        with open(self.model_state_file, 'wb') as f:
                            pickle.dump(state, f)
                        
                        # Update cache (deep copy for numpy arrays)
                        if isinstance(state, dict):
                            import copy
                            self._cached_state = copy.deepcopy(state)
                        else:
                            self._cached_state = state
                        self._cache_mtime = os.path.getmtime(self.model_state_file)
                        
                        logger.debug(f"Saved model state to {self.model_state_file}")
                    finally:
                        fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logger.error(f"Error saving model state: {e}")
    
    def load_state(self, force_reload: bool = False):
        """
        Load model state from file with file locking and in-memory caching.
        
        Args:
            force_reload: If True, bypass cache and reload from disk
        """
        if not self.model_state_file or not os.path.exists(self.model_state_file):
            return False
        
        # Check cache first (unless forced reload)
        if not force_reload and self._cached_state is not None:
            try:
                current_mtime = os.path.getmtime(self.model_state_file)
                # Use cache if file hasn't changed and cache is fresh
                if current_mtime == self._cache_mtime:
                    self._set_state(self._cached_state)
                    return True
            except OSError:
                # File might have been deleted, fall through to reload
                pass
        
        try:
            if IS_WINDOWS and msvcrt:
                # Windows file locking
                with open(self.lock_file, 'w') as lock_f:
                    try:
                        msvcrt.locking(lock_f.fileno(), msvcrt.LK_LOCK, 1)
                        
                        # Load model state
                        with open(self.model_state_file, 'rb') as f:
                            state = pickle.load(f)
                        
                        # Update cache (deep copy for numpy arrays)
                        if isinstance(state, dict):
                            import copy
                            self._cached_state = copy.deepcopy(state)
                        else:
                            self._cached_state = state
                        self._cache_mtime = os.path.getmtime(self.model_state_file)
                        
                        self._set_state(state)
                        logger.debug(f"Loaded model state from {self.model_state_file}")
                        return True
                    finally:
                        try:
                            msvcrt.locking(lock_f.fileno(), msvcrt.LK_UNLCK, 1)
                        except:
                            pass
            else:
                # Unix/Linux file locking
                with open(self.lock_file, 'w') as lock_f:
                    try:
                        fcntl.flock(lock_f.fileno(), fcntl.LOCK_SH)
                        
                        # Load model state
                        with open(self.model_state_file, 'rb') as f:
                            state = pickle.load(f)
                        
                        # Update cache (deep copy for numpy arrays)
                        if isinstance(state, dict):
                            import copy
                            self._cached_state = copy.deepcopy(state)
                        else:
                            self._cached_state = state
                        self._cache_mtime = os.path.getmtime(self.model_state_file)
                        
                        self._set_state(state)
                        logger.debug(f"Loaded model state from {self.model_state_file}")
                        return True
                    finally:
                        fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logger.warning(f"Error loading model state: {e}")
            return False
    
    def _get_state(self) -> Dict[str, Any]:
        """Get current model state as dictionary. Override in subclasses."""
        raise NotImplementedError("Subclasses must implement _get_state()")
    
    def _set_state(self, state: Dict[str, Any]):
        """Set model state from dictionary. Override in subclasses."""
        raise NotImplementedError("Subclasses must implement _set_state()")
    
    def update_with_persistence(self, system_metrics: Dict[str, Any], 
                               job_parameters: Dict[str, Any], 
                               observed_runtime: float,
                               saved_predicted_runtime: Optional[float] = None):
        """
        Update model with persistence and locking to prevent race conditions.
        This should be called instead of update() when persistence is enabled.
        
        Args:
            system_metrics: System metrics at job start
            job_parameters: Job parameter values
            observed_runtime: Actual runtime in seconds
            saved_predicted_runtime: Optional predicted runtime saved during job assignment (for consistent logging)
        """
        # Load latest state before updating
        self.load_state()
        
        # Perform the update
        self.update(system_metrics, job_parameters, observed_runtime, saved_predicted_runtime=saved_predicted_runtime)
        
        # Save updated state
        self.save_state()


def load_system_metrics_config() -> Dict[str, Any]:
    """
    Load system metrics configuration from JSON file.
    
    Returns:
        Dictionary containing system_metric_fields and normalization settings
    """
    try:
        with open(SYSTEM_METRICS_CONFIG_PATH, 'r') as f:
            config = json.load(f)
        return config
    except FileNotFoundError:
        logger.warning(f"System metrics config not found at {SYSTEM_METRICS_CONFIG_PATH}, "
                      "using default configuration")
        # Return default config
        return {
            "system_metric_fields": [
                "cpu_util", "ram_util", "ram_available", "ram_total",
                "idle_slots", "load_1min", "load_5min", "load_15min",
                "load_per_cpu", "disk_io_util", "cpu_cores", "cpu_threads",
                "cpu_freq_mhz"
            ],
            "normalization": {
                "cpu_freq_mhz": {"enabled": True, "divisor": 10000.0},
                "ram_total": {"enabled": True, "divisor": 100.0},
                "ram_available": {"enabled": True, "divisor": 100.0}
            }
        }
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing system metrics config: {e}, using default configuration")
        raise


class FeatureExtractor:
    """
    Extracts and encodes features from system_metrics and job_parameters
    into a numerical feature vector for the contextual bandit algorithms.
    """
    
    def __init__(self, config_parameters: Dict[str, List[Any]], 
                 system_metrics_config_path: Optional[str] = None):
        """
        Initialize feature extractor with parameter definitions.
        
        Args:
            config_parameters: Dictionary from config.json parameters section
                              e.g., {"epochs": [1, 2, 4], "optimizer": ["adam", "sgd"]}
            system_metrics_config_path: Optional path to system metrics config file.
                                       If None, uses default path.
        """
        self.config_parameters = config_parameters
        self.parameter_names = sorted(config_parameters.keys())
        self.feature_dim = None
        
        # Load system metrics configuration
        if system_metrics_config_path:
            global SYSTEM_METRICS_CONFIG_PATH
            original_path = SYSTEM_METRICS_CONFIG_PATH
            SYSTEM_METRICS_CONFIG_PATH = system_metrics_config_path
            self.system_metrics_config = load_system_metrics_config()
            SYSTEM_METRICS_CONFIG_PATH = original_path
        else:
            self.system_metrics_config = load_system_metrics_config()
        
        self._build_feature_mapping()
    
    def _build_feature_mapping(self):
        """Build mapping for encoding parameters into features."""
        # Load system metrics features from config file
        system_metric_features = self.system_metrics_config.get(
            "system_metric_fields", 
            [
                "cpu_util", "ram_util", "ram_available", "ram_total",
                "idle_slots", "load_1min", "load_5min", "load_15min",
                "load_per_cpu", "disk_io_util", "cpu_cores", "cpu_threads",
                "cpu_freq_mhz"
            ]
        )
        
        # Parameter features
        param_features = []
        for param_name in self.parameter_names:
            param_values = self.config_parameters[param_name]
            # Detect data type
            sample_value = param_values[0] if param_values else None
            
            if isinstance(sample_value, (int, float)):
                # Numerical parameter: use one-hot or normalized value
                param_features.append(f"{param_name}_value")
            elif isinstance(sample_value, str):
                # Categorical parameter: one-hot encoding
                for val in param_values:
                    param_features.append(f"{param_name}_{val}")
            elif isinstance(sample_value, bool):
                # Boolean parameter
                param_features.append(f"{param_name}_bool")
            else:
                # Default: treat as categorical
                for val in param_values:
                    param_features.append(f"{param_name}_{val}")
        
        # Total feature dimension
        self.feature_dim = len(system_metric_features) + len(param_features)
        self.system_metric_features = system_metric_features
        self.param_features = param_features
    
    def extract_features(self, system_metrics: Dict[str, Any], 
                        job_parameters: Dict[str, Any]) -> np.ndarray:
        """
        Extract feature vector from system_metrics and job_parameters.
        
        Args:
            system_metrics: Dictionary with system performance metrics
            job_parameters: Dictionary with job parameter values
            
        Returns:
            Feature vector as numpy array
        """
        features = []
        
        # Extract system metrics features
        normalization_config = self.system_metrics_config.get("normalization", {})
        
        for metric_name in self.system_metric_features:
            value = system_metrics.get(metric_name, 0.0)
            
            # Apply normalization if configured
            if metric_name in normalization_config:
                norm_config = normalization_config[metric_name]
                if norm_config.get("enabled", False):
                    divisor = norm_config.get("divisor", 1.0)
                    if divisor > 0:
                        value = value / divisor
            
            features.append(float(value))
        
        # Extract parameter features
        for param_name in self.parameter_names:
            param_value = job_parameters.get(param_name)
            param_config = self.config_parameters[param_name]
            
            if isinstance(param_value, (int, float)):
                # Numerical: normalize to [0, 1] based on min/max in config
                if param_config:
                    min_val = min(param_config)
                    max_val = max(param_config)
                    if max_val > min_val:
                        normalized = (param_value - min_val) / (max_val - min_val)
                    else:
                        normalized = 0.5
                    features.append(normalized)
                else:
                    features.append(0.0)
            elif isinstance(param_value, str):
                # Categorical: one-hot encoding
                for val in param_config:
                    features.append(1.0 if param_value == val else 0.0)
            elif isinstance(param_value, bool):
                # Boolean
                features.append(1.0 if param_value else 0.0)
            else:
                # Default: one-hot for any other type
                for val in param_config:
                    features.append(1.0 if param_value == val else 0.0)
        
        return np.array(features, dtype=np.float64)


class LinearContextualBandit(ModelPersistence):
    """
    Linear Contextual Bandit using ridge regression.
    Predicts runtime based on system metrics and job parameters.
    """
    
    def __init__(self, config_parameters: Dict[str, List[Any]], alpha: float = 1.0, 
                 state_file_path: Optional[str] = None):
        """
        Initialize linear contextual bandit.
        
        Args:
            config_parameters: Parameter definitions from config.json
            alpha: Regularization parameter for ridge regression
            state_file_path: Optional path to persist model state (for multi-process safety)
        """
        super().__init__()
        self.feature_extractor = FeatureExtractor(config_parameters)
        self.alpha = alpha
        self.d = self.feature_extractor.feature_dim
        
        # Ridge regression parameters
        self.A = alpha * np.eye(self.d)  # Regularization matrix
        self.b = np.zeros(self.d)  # Weight vector
        self.theta = np.zeros(self.d)  # Learned parameters
        
        # Set up persistence if path provided
        if state_file_path:
            self.set_persistence_path(state_file_path)
            # Try to load existing state
            self.load_state()
    
    def _get_state(self) -> Dict[str, Any]:
        """Get current model state."""
        return {
            'A': self.A,
            'b': self.b,
            'theta': self.theta,
            'alpha': self.alpha,
            'd': self.d
        }
    
    def _set_state(self, state: Dict[str, Any]):
        """Set model state from dictionary."""
        self.A = state.get('A', self.A)
        self.b = state.get('b', self.b)
        self.theta = state.get('theta', self.theta)
        self.alpha = state.get('alpha', self.alpha)
        self.d = state.get('d', self.d)
        
    def predict(self, system_metrics: Dict[str, Any], 
                job_parameters: Dict[str, Any], skip_state_load: bool = False) -> float:
        """
        Predict runtime for given system metrics and job parameters.
        
        Args:
            system_metrics: System performance metrics
            job_parameters: Job parameter values
            skip_state_load: If True, skip loading state (for batch predictions)
            
        Returns:
            Predicted runtime in seconds
        """
        # Load latest state before prediction (if persistence enabled and not skipped)
        if self.model_state_file and not skip_state_load:
            self.load_state()
        
        x = self.feature_extractor.extract_features(system_metrics, job_parameters)
        # Update theta from current A and b
        try:
            self.theta = np.linalg.solve(self.A, self.b)
        except np.linalg.LinAlgError:
            # Fallback if A is singular
            self.theta = np.linalg.pinv(self.A) @ self.b
        
        prediction = np.dot(self.theta, x)
        return max(0.0, prediction)  # Runtime should be non-negative
    
    def update(self, system_metrics: Dict[str, Any], 
               job_parameters: Dict[str, Any], 
               observed_runtime: float,
               saved_predicted_runtime: Optional[float] = None):
        """
        Update model with observed runtime.
        
        This uses online least squares (ridge regression) which automatically
        adjusts weights to minimize prediction error:
        - If predicted > actual: weights adjust to predict less next time
        - If predicted < actual: weights adjust to predict more next time
        
        The update equations solve: minimize sum of (y_observed - theta^T * x)^2
        
        Args:
            system_metrics: System metrics at job start
            job_parameters: Job parameter values
            observed_runtime: Actual runtime in seconds
            saved_predicted_runtime: Optional predicted runtime saved during job assignment (for consistent logging)
        """
        x = self.feature_extractor.extract_features(system_metrics, job_parameters)
        
        # Get current prediction before update (for logging)
        try:
            theta_old = np.linalg.solve(self.A, self.b)
        except np.linalg.LinAlgError:
            theta_old = np.linalg.pinv(self.A) @ self.b
        predicted_runtime = max(0.0, np.dot(theta_old, x))
        
        # Use saved predicted runtime if available (for consistency with scheduling log)
        predicted_runtime_for_logging = saved_predicted_runtime if saved_predicted_runtime and saved_predicted_runtime > 0 else predicted_runtime
        
        # Update ridge regression parameters
        # A accumulates x * x^T (feature covariance)
        # b accumulates y_observed * x (target-feature correlation)
        self.A += np.outer(x, x)
        self.b += observed_runtime * x
        
        # Recompute theta (weights) that minimize squared error
        # theta = A^{-1} * b gives the optimal weights
        try:
            self.theta = np.linalg.solve(self.A, self.b)
        except np.linalg.LinAlgError:
            self.theta = np.linalg.pinv(self.A) @ self.b
        
        # Log prediction error for transparency
        prediction_error = predicted_runtime_for_logging - observed_runtime
        if saved_predicted_runtime and saved_predicted_runtime > 0:
            logger.info(f"Linear Bandit update: predicted={predicted_runtime_for_logging:.2f}s (saved from scheduling), "
                       f"actual={observed_runtime:.2f}s, "
                       f"error={prediction_error:.2f}s "
                       f"({'over' if prediction_error > 0 else 'under'}-predicted)")
        else:
            logger.info(f"Linear Bandit update: predicted={predicted_runtime:.2f}s, "
                       f"actual={observed_runtime:.2f}s, "
                       f"error={prediction_error:.2f}s "
                       f"({'over' if prediction_error > 0 else 'under'}-predicted)")


class LinUCB(ModelPersistence):
    """
    Linear Upper Confidence Bound algorithm for contextual bandits.
    Uses confidence intervals for exploration-exploitation trade-off.
    """
    
    def __init__(self, config_parameters: Dict[str, List[Any]], 
                 alpha: float = 1.0, confidence: float = 0.95,
                 state_file_path: Optional[str] = None):
        """
        Initialize LinUCB.
        
        Args:
            config_parameters: Parameter definitions from config.json
            alpha: Regularization parameter
            confidence: Confidence level for UCB (typically 0.95)
            state_file_path: Optional path to persist model state (for multi-process safety)
        """
        super().__init__()
        self.feature_extractor = FeatureExtractor(config_parameters)
        self.alpha = alpha
        self.confidence = confidence
        self.d = self.feature_extractor.feature_dim
        
        # LinUCB parameters
        self.A = alpha * np.eye(self.d)
        self.b = np.zeros(self.d)
        self.theta = np.zeros(self.d)
        
        # Confidence parameter (derived from confidence level)
        # Using standard UCB formula: sqrt(alpha * log(t))
        # For simplicity, we use a fixed exploration parameter
        self.exploration_param = 1.0
        
        # Set up persistence if path provided
        if state_file_path:
            self.set_persistence_path(state_file_path)
            # Try to load existing state
            self.load_state()
    
    def _get_state(self) -> Dict[str, Any]:
        """Get current model state."""
        return {
            'A': self.A,
            'b': self.b,
            'theta': self.theta,
            'alpha': self.alpha,
            'd': self.d,
            'confidence': self.confidence,
            'exploration_param': self.exploration_param
        }
    
    def _set_state(self, state: Dict[str, Any]):
        """Set model state from dictionary."""
        self.A = state.get('A', self.A)
        self.b = state.get('b', self.b)
        self.theta = state.get('theta', self.theta)
        self.alpha = state.get('alpha', self.alpha)
        self.d = state.get('d', self.d)
        self.confidence = state.get('confidence', self.confidence)
        self.exploration_param = state.get('exploration_param', 1.0)
    
    def predict(self, system_metrics: Dict[str, Any], 
                job_parameters: Dict[str, Any], skip_state_load: bool = False) -> float:
        """
        Predict runtime with upper confidence bound.
        
        Args:
            system_metrics: System performance metrics
            job_parameters: Job parameter values
            skip_state_load: If True, skip loading state (for batch predictions)
        
        Returns:
            Predicted runtime (mean + confidence bound)
        """
        # Load latest state before prediction (if persistence enabled and not skipped)
        if self.model_state_file and not skip_state_load:
            self.load_state()
        
        x = self.feature_extractor.extract_features(system_metrics, job_parameters)
        
        # Update theta
        try:
            self.theta = np.linalg.solve(self.A, self.b)
        except np.linalg.LinAlgError:
            self.theta = np.linalg.pinv(self.A) @ self.b
        
        # Mean prediction
        mean_prediction = np.dot(self.theta, x)
        
        # Confidence bound: sqrt(x^T A^{-1} x)
        try:
            A_inv = np.linalg.inv(self.A)
            confidence_bound = self.exploration_param * np.sqrt(np.dot(x, A_inv @ x))
        except np.linalg.LinAlgError:
            A_inv = np.linalg.pinv(self.A)
            confidence_bound = self.exploration_param * np.sqrt(np.dot(x, A_inv @ x))
        
        # UCB: mean + confidence bound
        ucb_prediction = mean_prediction + confidence_bound
        
        return max(0.0, ucb_prediction)
    
    def update(self, system_metrics: Dict[str, Any], 
               job_parameters: Dict[str, Any], 
               observed_runtime: float,
               saved_predicted_runtime: Optional[float] = None):
        """
        Update model with observed runtime.
        
        Uses online least squares which automatically adjusts weights:
        - If predicted > actual: weights adjust to predict less next time
        - If predicted < actual: weights adjust to predict more next time
        
        Args:
            system_metrics: System metrics at job start
            job_parameters: Job parameter values
            observed_runtime: Actual runtime in seconds
            saved_predicted_runtime: Optional predicted runtime saved during job assignment (for consistent logging)
        """
        x = self.feature_extractor.extract_features(system_metrics, job_parameters)
        
        # Get current prediction before update (for logging)
        try:
            theta_old = np.linalg.solve(self.A, self.b)
        except np.linalg.LinAlgError:
            theta_old = np.linalg.pinv(self.A) @ self.b
        mean_prediction = np.dot(theta_old, x)
        
        # Calculate UCB prediction (what was used for job selection)
        try:
            A_inv = np.linalg.inv(self.A)
            confidence_bound = self.exploration_param * np.sqrt(np.dot(x, A_inv @ x))
        except np.linalg.LinAlgError:
            A_inv = np.linalg.pinv(self.A)
            confidence_bound = self.exploration_param * np.sqrt(np.dot(x, A_inv @ x))
        ucb_prediction = mean_prediction + confidence_bound
        
        # Use saved predicted runtime if available (for consistency with scheduling log)
        # Otherwise use calculated UCB prediction
        predicted_runtime_for_logging = saved_predicted_runtime if saved_predicted_runtime and saved_predicted_runtime > 0 else ucb_prediction
        
        # Update parameters
        self.A += np.outer(x, x)
        self.b += observed_runtime * x
        
        # Update exploration parameter (increases with more observations)
        # This encourages exploration early, exploitation later
        trace_A = np.trace(self.A)
        if trace_A > 0:
            self.exploration_param = np.sqrt(np.log(1 + trace_A / self.alpha))
        
        # Log prediction error (using saved prediction if available for consistency)
        prediction_error = predicted_runtime_for_logging - observed_runtime
        if saved_predicted_runtime and saved_predicted_runtime > 0:
            logger.info(f"LinUCB update: predicted={predicted_runtime_for_logging:.2f}s (saved from scheduling), "
                       f"actual={observed_runtime:.2f}s, "
                       f"error={prediction_error:.2f}s "
                       f"({'over' if prediction_error > 0 else 'under'}-predicted)")
        else:
            logger.info(f"LinUCB update: UCB_predicted={ucb_prediction:.2f}s (mean={mean_prediction:.2f}s + bound={confidence_bound:.2f}s), "
                       f"actual={observed_runtime:.2f}s, "
                       f"error={prediction_error:.2f}s "
                       f"({'over' if prediction_error > 0 else 'under'}-predicted)")


class ThompsonSampling(ModelPersistence):
    """
    Thompson Sampling for linear contextual bandits.
    Uses Bayesian approach with Gaussian priors.
    """
    
    def __init__(self, config_parameters: Dict[str, List[Any]], 
                 alpha: float = 1.0, noise_variance: float = 1.0,
                 state_file_path: Optional[str] = None):
        """
        Initialize Thompson Sampling.
        
        Args:
            config_parameters: Parameter definitions from config.json
            alpha: Prior precision (inverse variance of prior)
            noise_variance: Variance of observation noise
            state_file_path: Optional path to persist model state (for multi-process safety)
        """
        super().__init__()
        self.feature_extractor = FeatureExtractor(config_parameters)
        self.alpha = alpha
        self.noise_variance = noise_variance
        self.d = self.feature_extractor.feature_dim
        
        # Bayesian parameters
        # Prior: theta ~ N(0, alpha^{-1} I)
        # Posterior: theta ~ N(mu, Sigma)
        self.mu = np.zeros(self.d)  # Posterior mean
        self.Sigma = (1.0 / alpha) * np.eye(self.d)  # Posterior covariance
        self.A = alpha * np.eye(self.d)  # Precision matrix
        self.b = np.zeros(self.d)
        
        # Set up persistence if path provided
        if state_file_path:
            self.set_persistence_path(state_file_path)
            # Try to load existing state
            self.load_state()
    
    def _get_state(self) -> Dict[str, Any]:
        """Get current model state."""
        return {
            'A': self.A,
            'b': self.b,
            'mu': self.mu,
            'Sigma': self.Sigma,
            'alpha': self.alpha,
            'noise_variance': self.noise_variance,
            'd': self.d
        }
    
    def _set_state(self, state: Dict[str, Any]):
        """Set model state from dictionary."""
        self.A = state.get('A', self.A)
        self.b = state.get('b', self.b)
        self.mu = state.get('mu', self.mu)
        self.Sigma = state.get('Sigma', self.Sigma)
        self.alpha = state.get('alpha', self.alpha)
        self.noise_variance = state.get('noise_variance', self.noise_variance)
        self.d = state.get('d', self.d)
    
    def predict(self, system_metrics: Dict[str, Any], 
                job_parameters: Dict[str, Any], skip_state_load: bool = False) -> float:
        """
        Predict runtime using Thompson Sampling.
        Samples from posterior distribution of theta.
        
        Args:
            system_metrics: System performance metrics
            job_parameters: Job parameter values
            skip_state_load: If True, skip loading state (for batch predictions)
        
        Returns:
            Predicted runtime (sampled from posterior)
        """
        # Load latest state before prediction (if persistence enabled and not skipped)
        if self.model_state_file and not skip_state_load:
            self.load_state()
        
        x = self.feature_extractor.extract_features(system_metrics, job_parameters)
        
        # Update posterior parameters
        try:
            self.Sigma = np.linalg.inv(self.A)
            self.mu = self.Sigma @ self.b
        except np.linalg.LinAlgError:
            self.Sigma = np.linalg.pinv(self.A)
            self.mu = self.Sigma @ self.b
        
        # Sample theta from posterior: theta ~ N(mu, Sigma)
        try:
            theta_sample = np.random.multivariate_normal(self.mu, self.Sigma)
        except np.linalg.LinAlgError:
            # Fallback: use mean if sampling fails
            theta_sample = self.mu
        
        # Predict using sampled theta
        prediction = np.dot(theta_sample, x)
        
        return max(0.0, prediction)
    
    def update(self, system_metrics: Dict[str, Any], 
               job_parameters: Dict[str, Any], 
               observed_runtime: float,
               saved_predicted_runtime: Optional[float] = None):
        """
        Update posterior distribution with observed runtime.
        
        Uses Bayesian linear regression which automatically adjusts weights:
        - If predicted > actual: posterior mean shifts to predict less next time
        - If predicted < actual: posterior mean shifts to predict more next time
        
        Args:
            system_metrics: System metrics at job start
            job_parameters: Job parameter values
            observed_runtime: Actual runtime in seconds
            saved_predicted_runtime: Optional predicted runtime saved during job assignment (for consistent logging)
        """
        x = self.feature_extractor.extract_features(system_metrics, job_parameters)
        
        # Get current prediction before update (for logging)
        try:
            mu_old = np.linalg.solve(self.A, self.b)
        except np.linalg.LinAlgError:
            mu_old = np.linalg.pinv(self.A) @ self.b
        mean_prediction = np.dot(mu_old, x)
        
        # Use saved predicted runtime if available (for consistency with scheduling log)
        predicted_runtime_for_logging = saved_predicted_runtime if saved_predicted_runtime and saved_predicted_runtime > 0 else mean_prediction
        
        # Update precision matrix and mean
        # A_new = A_old + (1/sigma^2) * x * x^T
        # b_new = b_old + (1/sigma^2) * y * x
        precision_update = (1.0 / self.noise_variance) * np.outer(x, x)
        self.A += precision_update
        self.b += (1.0 / self.noise_variance) * observed_runtime * x
        
        # Update posterior (will be computed in next predict call)
        try:
            self.Sigma = np.linalg.inv(self.A)
            self.mu = self.Sigma @ self.b
        except np.linalg.LinAlgError:
            self.Sigma = np.linalg.pinv(self.A)
            self.mu = self.Sigma @ self.b
        
        # Log prediction error
        prediction_error = predicted_runtime_for_logging - observed_runtime
        if saved_predicted_runtime and saved_predicted_runtime > 0:
            logger.info(f"Thompson Sampling update: predicted={predicted_runtime_for_logging:.2f}s (saved from scheduling), "
                       f"actual={observed_runtime:.2f}s, "
                       f"error={prediction_error:.2f}s "
                       f"({'over' if prediction_error > 0 else 'under'}-predicted)")
        else:
            logger.info(f"Thompson Sampling update: predicted={mean_prediction:.2f}s, "
                       f"actual={observed_runtime:.2f}s, "
                       f"error={prediction_error:.2f}s "
                       f"({'over' if prediction_error > 0 else 'under'}-predicted)")


# Factory function to create bandit instances
def create_bandit(algorithm: str, config_parameters: Dict[str, List[Any]], 
                  state_file_path: Optional[str] = None, **kwargs) -> Any:
    """
    Factory function to create contextual bandit instance.
    
    Args:
        algorithm: One of "linear", "linucb", "thompson"
        config_parameters: Parameter definitions from config.json
        state_file_path: Optional path to persist model state (for multi-process safety)
        **kwargs: Additional arguments for specific algorithms
        
    Returns:
        Instance of the requested bandit algorithm
    """
    algorithm = algorithm.lower()
    
    if algorithm == "linear":
        return LinearContextualBandit(config_parameters, state_file_path=state_file_path, **kwargs)
    elif algorithm == "linucb":
        return LinUCB(config_parameters, state_file_path=state_file_path, **kwargs)
    elif algorithm == "thompson":
        return ThompsonSampling(config_parameters, state_file_path=state_file_path, **kwargs)
    else:
        raise ValueError(f"Unknown algorithm: {algorithm}. "
                         f"Choose from: 'linear', 'linucb', 'thompson'")

