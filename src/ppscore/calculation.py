from sklearn import tree
from sklearn import preprocessing
from sklearn.dummy import DummyRegressor, DummyClassifier
from sklearn.model_selection import cross_val_score
from sklearn.metrics import f1_score
from sklearn.compose import make_column_transformer, ColumnTransformer
import numpy as np
import pandas as pd
from pandas.api.types import (
    is_numeric_dtype,
    is_bool_dtype,
    is_categorical_dtype,
    is_string_dtype,
    is_datetime64_any_dtype,
    is_timedelta64_dtype,
)

# if the number is 4, then it is possible to detect patterns when there are at least 4 times the same observation. If the limit is increased, the minimum observations also increase. This is important, because this is the limit when sklearn will throw an error which will lead to a score of 0 if we catch it
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder

CV_ITERATIONS = 4

RANDOM_SEED = 587136

# if a numeric column has less than 15 unique values, it is inferred as categoric
# thus, the ppscore will use a classification
# this has important implications on the ppscore
# eg if you have 4 equal categories encoded 0, 1, 2, 3 and treat it as a regression
# then the baseline is 1 (median) which is okayish and a predictor will have a harder time
# to beat the baseline, thus the ppscore will be considerably lower
# if the column is encoded as category, then the baseline will be to always predict 0
# this baseline will be way easier to beat and thus result in a higher ppscore
NUMERIC_AS_CATEGORIC_BREAKPOINT = 15


def _calculate_model_cv_score_(df, target, feature, metric, model, cv, **kwargs):
    "Calculates the mean model score based on cross-validation"
    # Sources about the used methods:
    # https://scikit-learn.org/stable/modules/tree.html
    # https://scikit-learn.org/stable/modules/cross_validation.html
    # https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.cross_val_score.html

    # preprocess target
    # TODO: this has a risk of leaking information if a target encoder were to be used, or misleading a
    # TODO: tree if some classes are not observed in some training folds
    # TODO: we need to make pre-processing a part of CV pipeline

    if df[target].dtype == object:
        le = preprocessing.LabelEncoder()
        df[target] = le.fit_transform(df[target])
        target_series = df[target]
    else:
        target_series = df[target]

    # preprocess feature
    preprocess = None
    if df[feature].dtype == object:
        # Dealing with categorical feature here here:
        preprocess = ColumnTransformer(
            transformers=[("ct", OneHotEncoder(), [feature])]
        )

    # reshaping needed because there is only 1 feature: coerce to DataFrame
    feature_df = df[feature].to_frame()

    if preprocess is None:
        pipeline_model = model
    else:
        pipeline_model = make_pipeline(preprocess, model)

    # Pull the groups out if passed
    groups = kwargs.get("groups", None)

    # Run crossvalidation with the CV specified
    # Crossvalidation is stratifiedKFold for classification, KFold for regression
    # CV on one core (n_job=1; default) has shown to be fastest
    scores = cross_val_score(
        pipeline_model, feature_df, target_series, cv=cv, scoring=metric, groups=groups
    )

    return scores.mean()


def _normalized_mae_score(model_mae, naive_mae):
    """Normalizes the model MAE score, given the baseline score"""
    # # Value range of MAE is [0, infinity), 0 is best
    # 10, 5 >> 0 because worse than naive
    # 10, 20 >> 0.5
    # 5, 20 >> 0.75 = 1 - (mae/base_mae)
    if model_mae > naive_mae:
        return 0
    else:
        return 1 - (model_mae / naive_mae)


def _mae_normalizer(df, y, model_score, cv, **kwargs):
    """
    In case of MAE, calculates the baseline score for y and derives the PPS.

    """
    df["naive"] = df[y].median()
    # Re-write baseline score using DummyRegressor with median strategy
    baseline_regr = DummyRegressor(strategy="median")
    groups = kwargs.get("groups", None)
    baseline_scores_cv = cross_val_score(
        baseline_regr,
        df,
        df[y],
        cv=cv,
        scoring="neg_mean_absolute_error",
        groups=groups,
    )
    baseline_score = np.mean(np.abs(baseline_scores_cv))

    ppscore = _normalized_mae_score(abs(model_score), baseline_score)
    return ppscore, baseline_score


def _normalized_f1_score(model_f1, baseline_f1):
    """Normalizes the model F1 score, given the baseline score"""
    # # F1 ranges from 0 to 1
    # # 1 is best
    # 0.5, 0.7 = 0 because worse than naive
    # 0.75, 0.5 > 0.5
    #
    if model_f1 < baseline_f1:
        return 0
    else:
        scale_range = 1.0 - baseline_f1  # eg 0.3
        f1_diff = model_f1 - baseline_f1  # eg 0.1
        return f1_diff / scale_range  # 0.1/0.3 = 0.33


def _f1_normalizer(df, y, model_score, cv, **kwargs):
    """In case of F1, calculates the baseline score for y and derives the PPS."""
    baseline_clf = DummyClassifier(strategy="stratified")
    groups = kwargs.get("groups", None)
    baseline_scores_cv = cross_val_score(
        baseline_clf, df, df[y], cv=cv, scoring="f1_weighted", groups=groups
    )
    baseline_score = baseline_scores_cv.mean()
    ppscore = _normalized_f1_score(model_score, baseline_score)
    return ppscore, baseline_score


TASKS = {
    "regression": {
        "metric_name": "mean absolute error",
        "metric_key": "neg_mean_absolute_error",
        "model": tree.DecisionTreeRegressor(),
        "score_normalizer": _mae_normalizer,
    },
    "classification": {
        "metric_name": "weighted F1",
        "metric_key": "f1_weighted",
        "model": tree.DecisionTreeClassifier(),
        "score_normalizer": _f1_normalizer,
    },
    "predict_itself": {
        "metric_name": None,
        "metric_key": None,
        "model": None,
        "score_normalizer": None,
    },
    "predict_constant": {
        "metric_name": None,
        "metric_key": None,
        "model": None,
        "score_normalizer": None,
    },
    "predict_id": {
        "metric_name": None,
        "metric_key": None,
        "model": None,
        "score_normalizer": None,
    },
}


def _infer_task(df, x, y):
    """Returns str with the name of the inferred task based on the columns x and y"""
    if x == y:
        return "predict_itself"

    category_count = df[y].value_counts().count()
    if category_count == 1:
        return "predict_constant"
    if category_count == 2:
        return "classification"
    if category_count == len(df[y]) and (
        is_string_dtype(df[y]) or is_categorical_dtype(df[y])
    ):
        return "predict_id"
    if category_count <= NUMERIC_AS_CATEGORIC_BREAKPOINT and is_numeric_dtype(df[y]):
        return "classification"

    if is_bool_dtype(df[y]) or is_string_dtype(df[y]) or is_categorical_dtype(df[y]):
        return "classification"

    if is_datetime64_any_dtype(df[y]) or is_timedelta64_dtype(df[y]):
        raise Exception(
            f"The target column {y} has the dtype {df[y].dtype} which is not supported. A possible solution might be to convert {y} to a string column"
        )

    # this check needs to be after is_bool_dtype because bool is considered numeric by pandas
    if is_numeric_dtype(df[y]):
        return "regression"

    raise Exception(
        f"Could not infer a valid task based on the target {y}. The dtype {df[y].dtype} is not yet supported"
    )  # pragma: no cover


def _feature_is_id(df, x):
    """Returns Boolean if the feature column x is an ID"""
    if not (is_string_dtype(df[x]) or is_categorical_dtype(df[x])):
        return False

    category_count = df[x].value_counts().count()
    return category_count == len(df[x])


def _maybe_sample(df, sample):
    """
    Maybe samples the rows of the given df to have at most ``sample`` rows
    If sample is ``None`` or falsy, there will be no sampling.
    If the df has fewer rows than the sample, there will be no sampling.

    Parameters
    ----------
    df : pandas.DataFrame
        Dataframe that might be sampled
    sample : int or ``None``
        Number of rows to be sampled

    Returns
    -------
    pandas.DataFrame
        DataFrame after potential sampling
    """
    if sample and len(df) > sample:
        # this is a problem if x or y have more than sample=5000 categories
        # TODO: dont sample when the problem occurs and show warning
        df = df.sample(sample, random_state=RANDOM_SEED, replace=False)
    return df


def score(df, x, y, task=None, sample=5000, cv=None, **kwargs):
    """
    Calculate the Predictive Power Score (PPS) for "x predicts y"
    The score always ranges from 0 to 1 and is data-type agnostic.

    A score of 0 means that the column x cannot predict the column y better than a naive baseline model.
    A score of 1 means that the column x can perfectly predict the column y given the model.
    A score between 0 and 1 states the ratio of how much potential predictive power the model achieved compared to the baseline model.

    Parameters
    ----------
    df : pandas.DataFrame
        Dataframe that contains the columns x and y
    x : str
        Name of the column x which acts as the feature
    y : str
        Name of the column y which acts as the target
    task : str, default ``None``
        Name of the prediction task, e.g. ``classification`` or ``regression``
        If the task is not specified, it is infered based on the y column
        The task determines which model and evaluation score is used for the PPS
    sample : int or ``None``
        Number of rows for sampling. The sampling decreases the calculation time of the PPS.
        If ``None`` there will be no sampling.
    cv: iterable or sklearn-compatible cv object
        Crossvalidation strategy to be used. if `None`, cv defaults to:
         stratifiedKFold for classification, KFold for regression


    Returns
    -------
    Dict
        A dict that contains multiple fields about the resulting PPS.
        The dict enables introspection into the calculations that have been performed under the hood
    """

    if cv is None:

        # Did not pass any CV - fallback to defaults:
        # Crossvalidation is stratifiedKFold for classification, KFold for regression
        cv = CV_ITERATIONS
        df = df.sample(frac=1, random_state=RANDOM_SEED, replace=False)

    if x == y:
        task_name = "predict_itself"
    else:
        # TODO: log.warning when values have been dropped
        df = df[[x, y]].dropna()
        if len(df) == 0:
            raise Exception(
                "After dropping missing values, there are no valid rows left"
            )
        df = _maybe_sample(df, sample)

        if task is None:
            task_name = _infer_task(df, x, y)
        else:
            task_name = task

    task = TASKS[task_name]

    if task_name in ["predict_constant", "predict_itself"]:
        model_score = 1
        ppscore = 1
        baseline_score = 1
    elif task_name == "predict_id":  # target is id
        model_score = 0
        ppscore = 0
        baseline_score = 0
    elif _feature_is_id(df, x):
        model_score = 0
        ppscore = 0
        baseline_score = 0
    else:
        model_score = _calculate_model_cv_score_(
            df,
            target=y,
            feature=x,
            metric=task["metric_key"],
            model=task["model"],
            cv=cv,
            **kwargs,
        )
        ppscore, baseline_score = task["score_normalizer"](
            df, y, model_score, cv, **kwargs
        )

    return {
        "x": x,
        "y": y,
        "task": task_name,
        "ppscore": ppscore,
        "metric": task["metric_name"],
        "baseline_score": baseline_score,
        "model_score": abs(model_score),  # sklearn returns negative mae
        "model": task["model"],
    }


# def predictors(df, y, task=None, sorted=True):
#    pass


def matrix(df, output="df", **kwargs):
    """
    Calculate the Predictive Power Score (PPS) matrix for all columns in the dataframe

    Parameters
    ----------
    df : pandas.DataFrame
        The dataframe that contains the data
    output: str - potential values: "df", "dict"
        Control the type of the output. Either return a df or a dict with all the PPS dicts arranged by the target column
    kwargs:
        Other key-word arguments that shall be forwarded to the pps.score method

    Returns
    -------
    pandas.DataFrame or Dict
        Either returns a df or a dict with all the PPS dicts arranged by the target column. This can be influenced by the output argument
    """
    data = {}
    columns = list(df.columns)

    for target in columns:
        scores = []
        for feature in columns:
            # single_score = score(df, x=feature, y=target)["ppscore"]
            try:
                single_score = score(df, x=feature, y=target, **kwargs)["ppscore"]
            except:
                # TODO: log error
                single_score = 0
            scores.append(single_score)
        data[target] = scores

    if output == "df":
        matrix = pd.DataFrame.from_dict(data, orient="index")
        matrix.columns = columns
        return matrix
    else:  # output == "dict"
        return data
