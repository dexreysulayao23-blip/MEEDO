import pandas as pd
import numpy as np
from statsmodels.tsa.arima.model import ARIMA
from statsmodels.tsa.stattools import adfuller
from sklearn.metrics import mean_absolute_error, mean_absolute_percentage_error, mean_squared_error
from sklearn.model_selection import TimeSeriesSplit
import warnings
from datetime import datetime
import joblib
import matplotlib.pyplot as plt
import io
import base64
import os
import re
warnings.filterwarnings('ignore')

# Train/evaluate models only on this calendar year onward (system rollout baseline).
MIN_TRAINING_YEAR = 2022

class RevenueARIMAModel:
    def __init__(self, models_dir='models'):
        self.source_models = {}
        self.source_data = {}
        self.user_inputs_history = {}
        self.metrics_history = {}
        self.models_dir = models_dir
        # Populated by get_model_stats_through_year; cleared when a source's model is re-saved.
        self._stats_through_year_cache = {}

        # Best-effort load of previously trained models (if any)
        self._ensure_models_dir()
        self._load_models_from_disk()

    def _ensure_models_dir(self):
        try:
            os.makedirs(self.models_dir, exist_ok=True)
        except Exception:
            # Non-fatal; training will still work without persistence
            pass

    def _safe_source_filename(self, source: str) -> str:
        s = str(source or '').strip()
        if not s:
            s = 'UNKNOWN'
        s = re.sub(r'[^A-Za-z0-9._-]+', '_', s)
        return s

    def _model_path(self, source: str) -> str:
        return os.path.join(self.models_dir, f"arima_{self._safe_source_filename(source)}.pkl")

    def _invalidate_stats_ty_cache_for_source(self, source: str):
        """Drop cached CV-through-year stats when the fitted model for this source changes."""
        c = getattr(self, '_stats_through_year_cache', None)
        if not c:
            return
        for k in list(c.keys()):
            if k and k[0] == source:
                del c[k]

    def _save_model_to_disk(self, source: str):
        if source not in self.source_models:
            return
        try:
            payload = {
                'source': source,
                'model': self.source_models[source].get('model'),
                'data': self.source_models[source].get('data'),
                'params': self.source_models[source].get('params'),
                'mean': self.source_models[source].get('mean'),
                'std': self.source_models[source].get('std'),
                'last_training': self.source_models[source].get('last_training'),
                'metrics': self.metrics_history.get(source, {}),
            }
            joblib.dump(payload, self._model_path(source))
            try:
                self._invalidate_stats_ty_cache_for_source(source)
            except Exception:
                pass
        except Exception as e:
            print(f"[WARN] Could not persist model for {source}: {e}")

    def _load_models_from_disk(self):
        try:
            if not os.path.isdir(self.models_dir):
                return
            for name in os.listdir(self.models_dir):
                if not (name.startswith("arima_") and name.endswith(".pkl")):
                    continue
                path = os.path.join(self.models_dir, name)
                try:
                    payload = joblib.load(path)
                    source = payload.get('source')
                    model = payload.get('model')
                    data = payload.get('data')
                    if not source or model is None or data is None:
                        continue
                    self.source_models[source] = {
                        'model': model,
                        'data': data,
                        'params': payload.get('params', (1, 1, 1)),
                        'mean': payload.get('mean', 0),
                        'std': payload.get('std', 0),
                        'last_training': payload.get('last_training', datetime.now())
                    }
                    self.metrics_history[source] = payload.get('metrics', {}) or {}
                    self.source_data[source] = data
                except Exception:
                    continue
        except Exception:
            return
    
    def add_user_input(self, source, income_date, amount):
        """Add user input to history"""
        if source not in self.user_inputs_history:
            self.user_inputs_history[source] = []
        
        self.user_inputs_history[source].append({
            'date': income_date,
            'amount': amount,
            'timestamp': datetime.now()
        })
        
        # Keep only last 100 inputs
        if len(self.user_inputs_history[source]) > 100:
            self.user_inputs_history[source] = self.user_inputs_history[source][-100:]
        
        # Retrain every 10 inputs
        if len(self.user_inputs_history[source]) % 10 == 0 and len(self.user_inputs_history[source]) > 0:
            self.retrain_with_user_inputs(source)
        
        print(f"[OK] Added user input for {source}: PHP {amount:,.2f} on {income_date}")
    
    def retrain_with_user_inputs(self, source):
        """Retrain model incorporating user inputs"""
        if source not in self.source_models:
            return
        
        try:
            historical = self.source_data[source]['amount_remitted'].copy()
            
            user_inputs_df = pd.DataFrame(self.user_inputs_history[source])
            if not user_inputs_df.empty:
                user_inputs_df['date'] = pd.to_datetime(user_inputs_df['date'])
                user_inputs_df.set_index('date', inplace=True)
                user_inputs_df = user_inputs_df[['amount']].rename(columns={'amount': 'amount_remitted'})
                
                combined = pd.concat([historical, user_inputs_df['amount_remitted']])
                combined = combined[~combined.index.duplicated(keep='last')]
                combined = combined.sort_index()
                combined = combined.asfreq('MS')
                combined_pre = combined.astype(float).copy()
                combined, imp_meta = self._impute_amount_series(combined)
                try:
                    pos_ix = combined_pre[combined_pre > 0].index
                    if len(pos_ix):
                        imp_meta['observed_year_first'] = int(pos_ix.year.min())
                        imp_meta['observed_year_last'] = int(pos_ix.year.max())
                except Exception:
                    pass
                
                best_params = self.source_models[source].get('params', (1, 1, 1))
                model = ARIMA(combined, order=best_params)
                model_fit = model.fit()
                
                self.source_models[source] = {
                    'model': model_fit,
                    'data': pd.DataFrame({'amount_remitted': combined}),
                    'params': best_params,
                    'mean': float(combined.mean()),
                    'std': float(combined.std()),
                    'last_training': datetime.now()
                }

                # Update metrics so CV MAPE shown in UI isn't stale after learning.
                try:
                    in_pred = model_fit.predict(start=0, end=len(combined) - 1)
                    in_metrics = self.calculate_metrics(combined.values, in_pred.values)
                except Exception:
                    in_metrics = {'mape': None, 'rmse': None, 'mae': None}

                try:
                    cv_scores, cv_mape, cv_rmse = self._cross_validate(pd.Series(combined).astype(float), best_params, max_splits=5)
                except Exception:
                    cv_scores, cv_mape, cv_rmse = ({'mape': [], 'rmse': [], 'failed_folds': 0, 'n_splits': 0}, None, None)

                self.metrics_history[source] = {
                    **(self.metrics_history.get(source, {}) or {}),
                    'cv_scores': cv_scores,
                    'cv_mape': cv_mape,
                    'cv_rmse': cv_rmse,
                    'in_sample_mape': in_metrics.get('mape'),
                    'in_sample_rmse': in_metrics.get('rmse'),
                    'in_sample_mae': in_metrics.get('mae'),
                    'best_params': best_params,
                    'aic': getattr(model_fit, 'aic', None),
                    'bic': getattr(model_fit, 'bic', None),
                    'imputation': imp_meta,
                }

                # Persist updated metrics too
                self._save_model_to_disk(source)
                
                print(f"[OK] Retrained {source} model with {len(user_inputs_df)} user inputs")
                
        except Exception as e:
            print(f"Error retraining {source}: {e}")
    
    def get_model_stats(self, source):
        """Get statistics about the model"""
        if source not in self.source_models:
            return None
        
        model_info = self.source_models[source]
        user_inputs = self.user_inputs_history.get(source, [])
        metrics = self.metrics_history.get(source, {}) or {}

        # Some older persisted models may not have in-sample metrics saved.
        # If missing, compute them from the fitted model + stored series.
        try:
            if metrics.get('in_sample_mape', None) is None or metrics.get('in_sample_rmse', None) is None:
                series_df = model_info.get('data', None)
                if series_df is not None:
                    # model_info['data'] is typically a DataFrame with 'amount_remitted' column at monthly frequency
                    if hasattr(series_df, 'get') and series_df.get('amount_remitted', None) is not None:
                        series = series_df['amount_remitted'].astype(float)
                        if len(series) > 0 and model_info.get('model', None) is not None:
                            in_pred = model_info['model'].predict(start=0, end=len(series) - 1)
                            in_metrics = self.calculate_metrics(series.values, in_pred.values)
                            metrics['in_sample_mape'] = in_metrics.get('mape')
                            metrics['in_sample_rmse'] = in_metrics.get('rmse')
                            # Persist back so UI always has them after first compute
                            self.metrics_history[source] = {**self.metrics_history.get(source, {}), **metrics}
                            self._save_model_to_disk(source)
        except Exception:
            pass
        
        return {
            'source': source,
            'historical_data_points': len(model_info.get('data', [])),
            'user_inputs_count': len(user_inputs),
            'last_training': model_info.get('last_training', datetime.now()).strftime('%Y-%m-%d %H:%M'),
            'mean': model_info.get('mean', 0),
            'std': model_info.get('std', 0),
            # Accuracy metrics (requested in thesis objectives)
            'cv_mape': metrics.get('cv_mape', None),
            'cv_rmse': metrics.get('cv_rmse', None),
            'in_sample_mape': metrics.get('in_sample_mape', None),
            'in_sample_rmse': metrics.get('in_sample_rmse', None),
            'aic': metrics.get('aic', None),
            'bic': metrics.get('bic', None),
            'model_params': model_info.get('params', 'unknown'),
            'imputation': metrics.get('imputation'),
        }

    def get_model_stats_through_year(self, source, through_year):
        """
        Same as get_model_stats, but CV MAPE / CV RMSE are recomputed on the stored monthly
        series restricted to calendar years MIN_TRAINING_YEAR..through_year (inclusive).
        UI sends tracker year so metrics expand cumulatively (2022 → 2022 only; 2025 → 2022–2025).
        """
        base = self.get_model_stats(source)
        if base is None:
            return None
        try:
            ty = int(through_year)
        except Exception:
            return base
        ty = max(ty, int(MIN_TRAINING_YEAR))

        if source not in self.source_models:
            return base

        model_info = self.source_models[source]
        lt = model_info.get('last_training')
        try:
            lt_key = lt.isoformat() if hasattr(lt, 'isoformat') else str(lt)
        except Exception:
            lt_key = str(lt)
        ck = (source, int(ty), lt_key)
        if ck in self._stats_through_year_cache:
            return dict(self._stats_through_year_cache[ck])

        series_df = model_info.get('data')
        if series_df is None:
            return base
        try:
            col = series_df.get('amount_remitted') if hasattr(series_df, 'get') else None
            if col is None:
                return base
            s = col.astype(float).copy()
            if not isinstance(s.index, pd.DatetimeIndex):
                try:
                    s.index = pd.DatetimeIndex(pd.to_datetime(s.index))
                except Exception:
                    return base
            metrics_blob = self.metrics_history.get(source) or {}
            imp = metrics_blob.get('imputation') or {}
            obs_lo = imp.get('observed_year_first')
            obs_hi = imp.get('observed_year_last')
            try:
                y_lo = max(int(MIN_TRAINING_YEAR), int(obs_lo)) if obs_lo is not None else int(MIN_TRAINING_YEAR)
            except Exception:
                y_lo = int(MIN_TRAINING_YEAR)
            try:
                y_hi = min(int(ty), int(obs_hi)) if obs_hi is not None else int(ty)
            except Exception:
                y_hi = int(ty)
            if y_hi < y_lo:
                y_hi = y_lo
            mask = (s.index.year >= y_lo) & (s.index.year <= y_hi)
            sliced = s[mask]
        except Exception:
            return base

        # Label reflects calendar years actually present in the stored training series slice
        # (may end before `ty` if Excel/DB reload hasn't extended the fitted monthly index yet).
        def _span_lbl(series_slice):
            try:
                if series_slice is None or len(series_slice) == 0:
                    return None
                yrs = series_slice.index.year
                y_min, y_max = int(yrs.min()), int(yrs.max())
                return f'{y_min}–{y_max}' if y_min != y_max else str(y_min)
            except Exception:
                return None

        if sliced is None or len(sliced) < 5:
            out = dict(base)
            out['cv_through_year'] = ty
            out['cv_window_label'] = _span_lbl(sliced) or f'{MIN_TRAINING_YEAR}–{ty}'
            out['cv_mape'] = None
            out['cv_rmse'] = None
            if len(sliced) > 0:
                out['mean'] = float(np.nanmean(sliced.values))
            self._stats_through_year_cache[ck] = dict(out)
            return out

        order = model_info.get('params', (1, 1, 1))
        try:
            cv_scores, cv_mape, cv_rmse = self._cross_validate(sliced.astype(float), order, max_splits=5)
        except Exception:
            cv_scores, cv_mape, cv_rmse = ({'mape': [], 'rmse': [], 'failed_folds': 0, 'n_splits': 0}, None, None)

        out = dict(base)
        out['cv_through_year'] = ty
        out['cv_window_label'] = _span_lbl(sliced) or f'{MIN_TRAINING_YEAR}–{ty}'
        out['cv_mape'] = cv_mape
        out['cv_rmse'] = cv_rmse
        out['mean'] = float(np.nanmean(sliced.values))
        out['cv_scores_window'] = cv_scores
        out['historical_data_points_window'] = int(len(sliced))
        self._stats_through_year_cache[ck] = dict(out)
        return out

    def _same_month_cross_year_fill(self, s: pd.Series) -> pd.Series:
        """
        For each month with NaN or <=0, if any other year has the same calendar month with a positive
        amount in the original series, copy the nearest-by-year value (tie-break: earlier year).
        """
        out = s.astype(float).copy()
        orig = s.astype(float).copy()
        if not isinstance(out.index, pd.DatetimeIndex):
            try:
                out.index = pd.to_datetime(out.index)
                orig.index = out.index
            except Exception:
                return out
        out = out.sort_index()
        orig = orig.reindex(out.index)
        if not bool((orig > 0).any()):
            return out
        for ts in out.index:
            cur = float(out.loc[ts])
            if np.isfinite(cur) and cur > 0:
                continue
            m = int(ts.month)
            y = int(ts.year)
            cands = []
            for idx in orig.index:
                if idx == ts or int(idx.month) != m:
                    continue
                v = float(orig.loc[idx])
                if np.isfinite(v) and v > 0:
                    cands.append((abs(int(idx.year) - y), int(idx.year), v))
            if not cands:
                continue
            cands.sort(key=lambda x: (x[0], x[1]))
            out.loc[ts] = float(cands[0][2])
        return out

    def _impute_amount_series(self, amount_series: pd.Series):
        """
        Handle missing / unreported monthly amounts before ARIMA:
        1) Same calendar month in other years (nearest calendar year first), when available.
        2) Remaining NaNs / non-positive values are treated as missing when any positive exists.
        3) Time interpolation along the DatetimeIndex, then backward fill, then forward fill.
        Returns (filled_series, metadata dict).
        """
        if amount_series is None or len(amount_series) == 0:
            return amount_series, {
                'method': 'none',
                'months_gaps_filled': 0,
                'non_positive_as_missing': False,
                'raw_nan_months': 0,
                'raw_zero_months': 0,
                'note': 'No rows to preprocess.',
            }

        s = amount_series.astype(float).copy()
        if not isinstance(s.index, pd.DatetimeIndex):
            try:
                s.index = pd.to_datetime(s.index)
            except Exception:
                pass
        s = s.sort_index()

        raw_nan = int(s.isna().sum())
        raw_zero = int((s == 0).sum())
        has_positive = bool((s > 0).any())
        s = self._same_month_cross_year_fill(s)
        if has_positive:
            s_work = s.where(s > 0)
        else:
            s_work = s.copy()

        missing_pre = int(s_work.isna().sum())
        try:
            filled = s_work.interpolate(method='time', limit_direction='both')
        except Exception:
            filled = s_work.interpolate(method='linear', limit_direction='both')
        filled = filled.bfill().ffill()
        if filled.isna().any():
            pos_mean = float(s[s > 0].mean()) if (s > 0).any() else float(np.nanmean(s.to_numpy(dtype=float)))
            if not np.isfinite(pos_mean) or pos_mean <= 0:
                pos_mean = 1.0
            filled = filled.fillna(pos_mean)

        meta = {
            'method': 'same calendar month (other years) + time interpolation + bfill + ffill',
            'non_positive_as_missing': has_positive,
            'months_gaps_filled': int(missing_pre),
            'raw_nan_months': raw_nan,
            'raw_zero_months': raw_zero,
            'note': (
                'Zeros and gaps first borrow from the same month in other years in the series when possible; '
                'remaining gaps use time interpolation and forward/backward fill before ARIMA training.'
            ),
        }
        return filled, meta

    def prepare_time_series(self, data, source):
        """Prepare monthly time series data for ARIMA modeling. Returns (monthly_data, imputation_meta)."""
        source_df = data[data['source'] == source].copy()

        if source_df.empty:
            return None, {}

        # Use all fiscal history from MIN_TRAINING_YEAR onward so CV MAPE / RMSE reflect
        # multi-year patterns (not a single calendar year in the tracker UI).
        try:
            source_df['date'] = pd.to_datetime(source_df['date'])
            yr = source_df['date'].dt.year
            mask = yr >= int(MIN_TRAINING_YEAR)
            if bool(mask.any()):
                source_df = source_df.loc[mask].copy()
        except Exception:
            pass

        if source_df.empty:
            return None, {}

        # Aggregate by month
        source_df['year_month'] = source_df['date'].dt.to_period('M')
        monthly_data = source_df.groupby('year_month')['amount_remitted'].sum().reset_index()
        monthly_data['date'] = monthly_data['year_month'].dt.start_time
        # Years that have at least one positive monthly total before gap-fill (for UI/reporting parity)
        observed_year_first = None
        observed_year_last = None
        try:
            pos_m = monthly_data[monthly_data['amount_remitted'].astype(float) > 0]
            if not pos_m.empty:
                yrs = pd.to_datetime(pos_m['date']).dt.year
                observed_year_first = int(yrs.min())
                observed_year_last = int(yrs.max())
        except Exception:
            pass
        monthly_data.set_index('date', inplace=True)

        # Ensure regular monthly frequency (introduces NaNs for months with no rows)
        monthly_data = monthly_data.asfreq('MS')
        amt = monthly_data['amount_remitted'].astype(float)
        filled, imp_meta = self._impute_amount_series(amt)
        if observed_year_first is not None:
            imp_meta['observed_year_first'] = observed_year_first
        if observed_year_last is not None:
            imp_meta['observed_year_last'] = observed_year_last
        monthly_data['amount_remitted'] = filled

        # Add time-based features
        monthly_data['month'] = monthly_data.index.month
        monthly_data['quarter'] = monthly_data.index.quarter
        monthly_data['year'] = monthly_data.index.year

        # Feature engineering (derived features for analysis/insights)
        # Note: ARIMA training still uses the main series `amount_remitted`.
        s = monthly_data['amount_remitted'].astype(float)
        monthly_data['lag_1'] = s.shift(1)
        monthly_data['lag_3'] = s.shift(3)
        monthly_data['lag_6'] = s.shift(6)
        monthly_data['lag_12'] = s.shift(12)

        monthly_data['rolling_mean_3'] = s.rolling(window=3, min_periods=1).mean()
        monthly_data['rolling_mean_6'] = s.rolling(window=6, min_periods=1).mean()
        monthly_data['rolling_mean_12'] = s.rolling(window=12, min_periods=1).mean()
        monthly_data['rolling_std_3'] = s.rolling(window=3, min_periods=1).std().fillna(0.0)
        monthly_data['rolling_std_6'] = s.rolling(window=6, min_periods=1).std().fillna(0.0)
        monthly_data['rolling_std_12'] = s.rolling(window=12, min_periods=1).std().fillna(0.0)

        # Growth rates (for diagnostics and decision support insights)
        monthly_data['mom_growth_pct'] = s.pct_change(periods=1) * 100.0
        monthly_data['qoq_growth_pct'] = s.pct_change(periods=3) * 100.0
        monthly_data['yoy_growth_pct'] = s.pct_change(periods=12) * 100.0

        return monthly_data, imp_meta
    
    def check_stationarity(self, series):
        """Check if time series is stationary using ADF test"""
        result = adfuller(series.dropna())
        return {
            'is_stationary': result[1] < 0.05,
            'p_value': result[1],
            'test_statistic': result[0]
        }
    
    def find_best_arima_params(self, series, max_p=5, max_d=2, max_q=5):
        """Find best ARIMA parameters using AIC"""
        best_aic = float('inf')
        best_params = None
        
        print(f"    Searching ARIMA orders: p(0-{max_p}), d(0-{max_d}), q(0-{max_q})")
        
        for p in range(max_p + 1):
            for d in range(max_d + 1):
                for q in range(max_q + 1):
                    try:
                        model = ARIMA(series, order=(p, d, q))
                        model_fit = model.fit()
                        if model_fit.aic < best_aic:
                            best_aic = model_fit.aic
                            best_params = (p, d, q)
                            print(f"      Found better: ARIMA{(p,d,q)} with AIC={best_aic:.2f}")
                    except:
                        continue
        
        if best_params is None:
            best_params = (1, 1, 1)
            print("  [WARN] Using fallback ARIMA(1,1,1)")
        
        return best_params, best_aic
    
    def calculate_metrics(self, actual, predicted):
        """Calculate MAE, MAPE and RMSE"""
        mask = ~(np.isnan(actual) | np.isnan(predicted))
        actual_clean = actual[mask]
        predicted_clean = predicted[mask]
        
        if len(actual_clean) == 0:
            return {'mae': float('inf'), 'mape': float('inf'), 'rmse': float('inf')}
        
        mae = float(mean_absolute_error(actual_clean, predicted_clean))

        # MAPE with epsilon-stabilized denominator for zeros
        epsilon = 1e-10
        denom = np.maximum(np.abs(actual_clean), epsilon)
        mape = float(np.mean(np.abs((actual_clean - predicted_clean) / denom)) * 100.0)
        mape = min(mape, 200.0)
        rmse = np.sqrt(mean_squared_error(actual_clean, predicted_clean))
        
        return {
            'mae': mae,
            'mape': mape,
            'rmse': rmse
        }

    def _cross_validate(self, series: pd.Series, order, max_splits: int = 5):
        """
        Time-series cross-validation for a single univariate series.
        Returns:
          - cv_scores: {'mape': [...], 'rmse': [...], 'failed_folds': int, 'n_splits': int}
          - cv_mape: float|None
          - cv_rmse: float|None
        Notes:
          - Skips CV when there are too few months.
          - Failed folds are excluded from averages (tracked via failed_folds).
        """
        try:
            n = int(len(series))
        except Exception:
            n = 0

        # Previous rule was n_splits=min(5, len(series)-2). Keep it, but guard lower bound.
        n_splits = min(int(max_splits), n - 2)
        if n_splits < 2:
            return {'mape': [], 'rmse': [], 'failed_folds': 0, 'n_splits': 0}, None, None

        tscv = TimeSeriesSplit(n_splits=n_splits)
        cv_scores = {'mape': [], 'rmse': [], 'failed_folds': 0, 'n_splits': n_splits}

        for _, (train_idx, test_idx) in enumerate(tscv.split(series), 1):
            train = series.iloc[train_idx]
            test = series.iloc[test_idx]
            try:
                model = ARIMA(train, order=order)
                model_fit = model.fit()
                predictions = model_fit.forecast(steps=len(test))
                metrics = self.calculate_metrics(test.values, predictions.values)
                cv_scores['mape'].append(float(metrics.get('mape', float('inf'))))
                cv_scores['rmse'].append(float(metrics.get('rmse', float('inf'))))
            except Exception:
                cv_scores['failed_folds'] += 1
                cv_scores['mape'].append(float('inf'))
                cv_scores['rmse'].append(float('inf'))

        valid_mape = [m for m in cv_scores['mape'] if np.isfinite(m)]
        valid_rmse = [r for r in cv_scores['rmse'] if np.isfinite(r)]
        cv_mape = float(np.mean(valid_mape)) if valid_mape else None
        cv_rmse = float(np.mean(valid_rmse)) if valid_rmse else None
        return cv_scores, cv_mape, cv_rmse
    
    def train_model(self, data, source):
        """Train ARIMA model with monthly data"""
        print(f"\n{'='*60}")
        print(f"TRAINING ARIMA MODEL FOR: {source}")
        print('='*60)
        
        # Prepare monthly time series data (missing / zero gaps imputed for modeling)
        monthly_data, imp_meta = self.prepare_time_series(data, source)

        if monthly_data is None or monthly_data.empty:
            print(f"[WARN] No data for {source}, skipping...")
            return None, None, None

        series = monthly_data['amount_remitted']
        if imp_meta.get('months_gaps_filled', 0):
            print(
                f"\n[INFO] Missing-value handling: {imp_meta.get('method')} — "
                f"gaps filled: {imp_meta.get('months_gaps_filled')} "
                f"(raw NaN months: {imp_meta.get('raw_nan_months')}, "
                f"raw zero months: {imp_meta.get('raw_zero_months')}, "
                f"non-positive treated as missing: {imp_meta.get('non_positive_as_missing')})"
            )
        
        # Store data
        self.source_data[source] = monthly_data
        self.user_inputs_history[source] = []
        
        print("\n[INFO] Data Summary:")
        print(f"  Total months: {len(series)}")
        print(f"  Date range: {series.index.min()} to {series.index.max()}")
        print(f"  Mean revenue: PHP {series.mean():,.2f}")
        print(f"  Std deviation: PHP {series.std():,.2f}")
        print(f"  Min revenue: PHP {series.min():,.2f}")
        print(f"  Max revenue: PHP {series.max():,.2f}")
        
        # Check stationarity
        stationarity = self.check_stationarity(series)
        print("\n[INFO] Stationarity Test:")
        print(f"  ADF p-value: {stationarity['p_value']:.6f}")
        print(f"  Is stationary: {stationarity['is_stationary']}")
        
        # Find best parameters
        print(f"\n[INFO] Finding best ARIMA parameters for {source}...")
        best_params, best_aic = self.find_best_arima_params(series, max_p=5, max_d=2, max_q=5)
        
        print(f"\n[OK] Best ARIMA order for {source}: {best_params}")
        print(f"   Best AIC: {best_aic:.2f}")
        
        # Time series cross-validation
        print("\n[INFO] Performing Time Series Cross-Validation...")
        cv_scores, cv_mape, cv_rmse = self._cross_validate(series.astype(float), best_params, max_splits=5)
        if cv_scores.get('n_splits', 0) >= 2:
            for i, (m, r) in enumerate(zip(cv_scores['mape'], cv_scores['rmse']), 1):
                if np.isfinite(m) and np.isfinite(r):
                    print(f"  Fold {i}: MAPE={m:.2f}%, RMSE=PHP {r:,.2f}")
                else:
                    print(f"  Fold {i} failed")
        if cv_mape is not None:
            finite_mape = [m for m in cv_scores.get('mape', []) if np.isfinite(m)]
            print("\n[INFO] Cross-Validation Summary:")
            print(f"  Average MAPE: {cv_mape:.2f}% (+/- {float(np.std(finite_mape)):.2f})" if finite_mape else f"  Average MAPE: {cv_mape:.2f}%")
            if cv_rmse is not None:
                print(f"  Average RMSE: PHP {cv_rmse:,.2f}")
            if cv_scores.get('failed_folds', 0):
                print(f"  Failed folds: {cv_scores.get('failed_folds')}/{cv_scores.get('n_splits')}")
        else:
            print("  [INFO] CV skipped (not enough months) or all folds failed.")
        
        # Train final model on all data
        print("\n[INFO] Training final model on all data...")
        final_model = ARIMA(series, order=best_params)
        final_model_fit = final_model.fit()
        
        # Calculate in-sample metrics
        in_sample_pred = final_model_fit.predict(start=0, end=len(series)-1)
        in_sample_metrics = self.calculate_metrics(series.values, in_sample_pred.values)
        
        print("\n[INFO] Final Model Performance:")
        print(f"  In-sample MAPE: {in_sample_metrics['mape']:.2f}%")
        print(f"  In-sample RMSE: PHP {in_sample_metrics['rmse']:,.2f}")
        print(f"  Model AIC: {final_model_fit.aic:.2f}")
        print(f"  Model BIC: {final_model_fit.bic:.2f}")
        
        # Store model and metrics
        self.source_models[source] = {
            'model': final_model_fit,
            'data': monthly_data,
            'params': best_params,
            'mean': float(series.mean()),
            'std': float(series.std()),
            'last_training': datetime.now()
        }
        
        self.metrics_history[source] = {
            'cv_scores': cv_scores,
            'cv_mape': cv_mape,
            'cv_rmse': cv_rmse,
            'in_sample_mape': in_sample_metrics['mape'],
            'in_sample_rmse': in_sample_metrics['rmse'],
            'in_sample_mae': in_sample_metrics.get('mae'),
            'best_params': best_params,
            'aic': final_model_fit.aic,
            'bic': final_model_fit.bic,
            'imputation': imp_meta,
        }

        # Persist to disk for faster restarts
        self._save_model_to_disk(source)
        
        return final_model_fit, best_params, in_sample_metrics
    
    def train_models(self, data):
        """Train models for all income sources"""
        results = {}
        unique_sources = data['source'].unique()
        trained_sources = set()
        
        for source in unique_sources:
            if source in trained_sources:
                print(f"[WARN] Skipping duplicate source: {source}")
                continue
                
            try:
                model, params, metrics = self.train_model(data, source)
                if model is not None:
                    results[source] = {
                        'success': True,
                        'parameters': params,
                        'mae': metrics.get('mae'),
                        'mape': metrics['mape'],
                        'rmse': metrics['rmse'],
                        'data_points': len(self.source_data.get(source, []))
                    }
                    trained_sources.add(source)
                else:
                    results[source] = {
                        'success': False,
                        'message': 'No data available for this source'
                    }
            except Exception as e:
                print(f"[ERROR] Error training {source}: {e}")
                results[source] = {
                    'success': False,
                    'message': str(e)
                }
        
        return results
    
    def predict_revenue(self, source, current_monthly):
        """Predict revenue - MONTHLY and YEARLY only"""
        prediction = self.predict_future(source, months=12)
        
        if not prediction or not prediction.get('success'):
            return self._enhanced_simple_prediction(source, current_monthly)
        
        # Get historical average for ratio calculation
        historical_avg = self.source_models.get(source, {}).get('mean', current_monthly)
        
        # Calculate adjustment ratio based on user input vs historical average
        if historical_avg > 0:
            adjustment_ratio = current_monthly / historical_avg
            # Cap ratio to avoid extreme predictions
            adjustment_ratio = max(0.5, min(adjustment_ratio, 2.0))
        else:
            adjustment_ratio = 1.0
        
        # Adjust ARIMA predictions based on user's current input
        adjusted_monthly = prediction['aggregated']['monthly'] * adjustment_ratio
        adjusted_yearly = prediction['aggregated']['yearly'] * adjustment_ratio
        
        # Adjust monthly predictions for the chart
        adjusted_predictions = []
        for pred in prediction['predictions'][:12]:
            adjusted_predictions.append({
                'month': pred['month'],
                'date': pred['date'],
                'predicted': pred['predicted'] * adjustment_ratio,
                'lower': pred['lower'] * adjustment_ratio,
                'upper': pred['upper'] * adjustment_ratio
            })
        
        return {
            'success': True,
            'source': source,
            'current_monthly': current_monthly,
            'predicted_monthly': adjusted_monthly,
            'predicted_yearly': adjusted_yearly,
            'monthly_predictions': adjusted_predictions,
            'historical_avg': historical_avg,
            'confidence_score': prediction.get('confidence', 0.7),
            'ratio': adjustment_ratio,
            'user_inputs_count': len(self.user_inputs_history.get(source, [])),
            'model_trained': self.source_models.get(source, {}).get('last_training', datetime.now()).strftime('%Y-%m-%d %H:%M')
        }
    
    def _enhanced_simple_prediction(self, source, current_monthly):
        """Enhanced simple prediction with monthly and yearly only"""
        mi = self.source_models.get(source) if source in self.source_models else None
        month_starts = self._forecast_calendar_month_starts(mi, 12)
        monthly_predictions = []
        for i in range(12):
            # Gentle growth factor (0.5% per month)
            factor = 1 + (0.005 * i)
            pred = current_monthly * factor
            monthly_predictions.append({
                'month': i + 1,
                'date': month_starts[i].strftime('%Y-%m'),
                'predicted': float(pred),
                'lower': float(pred * 0.7),
                'upper': float(pred * 1.3)
            })
        
        monthly_pred = float(current_monthly)
        yearly_pred = float(current_monthly * 12 * 1.05)  # 5% annual growth assumption
        
        return {
            'success': True,
            'source': source,
            'current_monthly': current_monthly,
            'predicted_monthly': monthly_pred,
            'predicted_yearly': yearly_pred,
            'monthly_predictions': monthly_predictions,
            'historical_avg': float(current_monthly * 1.2),
            'confidence_score': 0.7,
            'ratio': 1.0,
            'user_inputs_count': 0,
            'note': 'Using enhanced prediction model'
        }
    
    def predict_auto(self, source):
        """Auto-predict revenue based on historical data without user input"""
        try:
            # Check if we have a trained model for this source
            if source in self.source_models:
                try:
                    # Get latest prediction using the trained model
                    prediction = self.predict_future(source, months=36)
                    
                    if prediction and prediction.get('success'):
                        # Get historical average from the model data
                        model_info = self.source_models[source]
                        historical_avg = model_info.get('mean', 50000)
                        
                        # Get current monthly from the last data point
                        current_monthly = historical_avg
                        if source in self.source_data:
                            source_data = self.source_data[source]
                            if source_data is not None and len(source_data) > 0:
                                try:
                                    if isinstance(source_data, pd.DataFrame):
                                        if 'amount_remitted' in source_data.columns:
                                            current_monthly = float(source_data['amount_remitted'].iloc[-1])
                                        else:
                                            current_monthly = float(source_data.iloc[:, 0].iloc[-1])
                                    elif isinstance(source_data, pd.Series):
                                        current_monthly = float(source_data.iloc[-1])
                                except:
                                    current_monthly = historical_avg
                        
                        # Get monthly predictions for chart
                        monthly_predictions = []
                        for i, pred in enumerate(prediction['predictions'][:36]):
                            monthly_predictions.append({
                                'month': i + 1,
                                'date': pred['date'],
                                'predicted': float(pred['predicted']),
                                'lower': float(pred.get('lower', pred['predicted'] * 0.8)),
                                'upper': float(pred.get('upper', pred['predicted'] * 1.2))
                            })
                        
                        return {
                            'success': True,
                            'source': source,
                            'current_monthly': current_monthly,
                            'predicted_monthly': float(prediction['predictions'][0]['predicted'] if prediction['predictions'] else 0),
                            'predicted_yearly': float(prediction['aggregated']['yearly']),
                            'monthly_predictions': monthly_predictions,
                            'historical_avg': historical_avg,
                            'confidence_score': prediction.get('confidence', 0.7),
                            'ratio': current_monthly / historical_avg if historical_avg > 0 else 1.0,
                            'auto_generated': True
                        }
                except Exception as e:
                    print(f"Error in predict_auto for {source}: {e}")
                    # Fall through to simple prediction
            
            # Fallback to enhanced simple prediction
            return self._enhanced_simple_prediction_auto(source)
            
        except Exception as e:
            print(f"Critical error in predict_auto: {e}")
            return self._enhanced_simple_prediction_auto(source)
    
    def _enhanced_simple_prediction_auto(self, source):
        """Enhanced simple prediction without user input for auto-predict"""
        # Get historical average from model if available
        historical_avg = 0
        if source in self.source_models:
            historical_avg = self.source_models[source].get('mean', 0)
        
        # If no historical data, use a reasonable default based on source
        if historical_avg == 0:
            # Set default values based on source type
            if 'BUS' in source:
                historical_avg = 18000
            elif 'DELIVERY' in source:
                historical_avg = 21000
            elif 'TOILET' in source:
                historical_avg = 18000
            elif 'STREET' in source:
                historical_avg = 22000
            elif 'MARKET-RENTAL' in source:
                historical_avg = 96000
            elif 'MARKET ELECTRIC' in source:
                historical_avg = 12000
            else:
                historical_avg = 15000
        
        current_monthly = historical_avg
        
        mi = self.source_models.get(source) if source in self.source_models else None
        month_starts = self._forecast_calendar_month_starts(mi, 36)
        monthly_predictions = []
        for i in range(36):
            factor = 1 + (0.005 * i)  # 0.5% growth per month
            pred = historical_avg * factor
            monthly_predictions.append({
                'month': i + 1,
                'date': month_starts[i].strftime('%Y-%m'),
                'predicted': float(pred),
                'lower': float(pred * 0.7),
                'upper': float(pred * 1.3)
            })
        
        return {
            'success': True,
            'source': source,
            'current_monthly': current_monthly,
            'predicted_monthly': float(current_monthly),
            'predicted_yearly': float(historical_avg * 12 * 1.05),
            'monthly_predictions': monthly_predictions,
            'historical_avg': historical_avg,
            'confidence_score': 0.7,
            'ratio': 1.0,
            'auto_generated': True,
            'note': 'Auto-generated prediction based on historical data'
        }
    
    def _seasonal_shape_factors_from_training(self, model_info):
        """
        One multiplier per calendar month (1-12) vs training mean, renormalized to average 1.0.
        Non-seasonal ARIMA long-horizon forecasts are often nearly flat; shaping restores realistic
        month-to-month movement aligned with historical seasonality in the training series.
        """
        try:
            data = model_info.get('data')
            if data is None or not hasattr(data, 'index') or len(data.index) < 6:
                return None
            col = data.get('amount_remitted') if hasattr(data, 'get') else None
            if col is None:
                return None
            s = col.astype(float)
            mu = float(np.nanmean(s.values))
            if not np.isfinite(mu) or mu <= 0:
                return None
            idx = pd.DatetimeIndex(pd.to_datetime(data.index))
            by_m = s.groupby(idx.month).mean()
            factors = {}
            for m in range(1, 13):
                if m in by_m.index:
                    v = float(by_m.loc[m])
                    factors[m] = (v / mu) if np.isfinite(v) and v > 0 else 1.0
                else:
                    factors[m] = 1.0
            avg_f = sum(factors[m] for m in range(1, 13)) / 12.0
            if avg_f > 0:
                for m in range(1, 13):
                    factors[m] /= avg_f
            # Avoid extreme spikes when few samples per month
            for m in range(1, 13):
                factors[m] = float(np.clip(factors[m], 0.65, 1.55))
            avg_f2 = sum(factors[m] for m in range(1, 13)) / 12.0
            if avg_f2 > 0:
                for m in range(1, 13):
                    factors[m] /= avg_f2
            return factors
        except Exception:
            return None

    def _forecast_calendar_month_starts(self, model_info, months):
        """Calendar month for each forecast step: step 0 = first month after the training sample end (matches statsmodels)."""
        try:
            data = model_info.get('data') if model_info else None
            if data is not None and hasattr(data, 'index') and len(data.index) > 0:
                last = pd.Timestamp(data.index[-1])
                start = last + pd.DateOffset(months=1)
                return [start + pd.DateOffset(months=i) for i in range(months)]
        except Exception:
            pass
        now = datetime.now()
        start = pd.Timestamp(now.year, now.month, 1) + pd.DateOffset(months=1)
        return [start + pd.DateOffset(months=i) for i in range(months)]

    def predict_future(self, source, months=12, confidence_level=0.95):
        """Make future predictions with confidence intervals"""
        if source not in self.source_models:
            return {'success': False, 'error': 'Model not found'}
        
        model_info = self.source_models[source]
        
        try:
            # ARIMA forecast
            forecast = model_info['model'].forecast(steps=months)
            
            # Get confidence intervals
            try:
                forecast_results = model_info['model'].get_forecast(steps=months)
                confidence = forecast_results.conf_int(alpha=1-confidence_level)
                has_ci = True
            except:
                has_ci = False
                confidence = None
            
            historical_metrics = self.metrics_history.get(source, {})
            month_starts = self._forecast_calendar_month_starts(model_info, months)
            shape = self._seasonal_shape_factors_from_training(model_info)

            predictions = []
            for i in range(months):
                pred_raw = float(max(0, forecast.iloc[i]))
                cal_m = int(month_starts[i].month)
                fshape = float(shape.get(cal_m, 1.0)) if shape else 1.0
                pred = float(max(0, pred_raw * fshape))

                if has_ci and confidence is not None and i < len(confidence):
                    lo = float(max(0, confidence.iloc[i, 0]))
                    up = float(max(0, confidence.iloc[i, 1]))
                    lower = float(max(0, lo * fshape))
                    upper = float(max(0, up * fshape))
                else:
                    lower = float(max(0, pred * 0.7))
                    upper = float(max(0, pred * 1.3))

                predictions.append({
                    'month': i + 1,
                    'date': month_starts[i].strftime('%Y-%m'),
                    'predicted': pred,
                    'lower': lower,
                    'upper': upper
                })
            
            monthly_pred = predictions[0]['predicted'] if predictions else 0
            yearly_pred = sum(p['predicted'] for p in predictions[:12])
            
            # Confidence score based on CV MAPE
            cv_mape = historical_metrics.get('cv_mape', 30)
            confidence_score = max(0.5, min(0.95, 1 - (cv_mape / 100)))
            
            return {
                'success': True,
                'source': source,
                'predictions': predictions,
                'confidence': confidence_score,
                'aggregated': {
                    'monthly': float(monthly_pred),
                    'yearly': float(yearly_pred)
                },
                'metrics': {
                    'cv_mape': historical_metrics.get('cv_mape', None),
                    'cv_rmse': historical_metrics.get('cv_rmse', None),
                    'in_sample_mape': historical_metrics.get('in_sample_mape', None),
                    'model_aic': historical_metrics.get('aic', None)
                },
                'last_training': model_info['last_training'].strftime('%Y-%m-%d %H:%M')
            }
            
        except Exception as e:
            print(f"Prediction error for {source}: {e}")
            return {'success': False, 'error': str(e)}