"""
Plotly helpers for the admin dashboards (reference data-viz palette).

One measure per chart (never a dual axis), a single hue per single-series chart,
thin marks with rounded data ends, recessive grid, hover on every mark. Dark
mode uses the palette's dark step, not an automatic inversion.
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go
import streamlit as st

SERIES = {"light": "#2a78d6", "dark": "#3987e5"}  # categorical slot 1 (blue)
INK = {"light": ("#0b0b0b", "#52514e"), "dark": ("#ffffff", "#c3c2b7")}
GRID = {"light": "rgba(11,11,11,0.08)", "dark": "rgba(255,255,255,0.10)"}


def _mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


def _layout(fig: go.Figure, *, height: int, y_title: str = "", x_title: str = "") -> go.Figure:
    mode = _mode()
    primary, secondary = INK[mode]
    fig.update_layout(
        height=height,
        margin={"l": 8, "r": 8, "t": 8, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": secondary, "size": 12},
        showlegend=False,
        bargap=0.35,
        barcornerradius=4,
        hoverlabel={"font": {"color": primary}},
    )
    axis = {
        "gridcolor": GRID[mode],
        "zeroline": False,
        "linecolor": GRID[mode],
        "tickfont": {"color": secondary},
    }
    fig.update_xaxes(**axis, title_text=x_title)
    fig.update_yaxes(**axis, title_text=y_title)
    return fig


def bars(
    labels: list[Any],
    values: list[float],
    *,
    horizontal: bool = False,
    value_format: str = ",.0f",
    hover_label: str = "",
    height: int = 260,
) -> None:
    color = SERIES[_mode()]
    hover = f"%{{{'y' if horizontal else 'x'}}}<br>{hover_label}%{{{'x' if horizontal else 'y'}:{value_format}}}<extra></extra>"
    trace = (
        go.Bar(x=values, y=labels, orientation="h", marker={"color": color}, hovertemplate=hover)
        if horizontal
        else go.Bar(x=labels, y=values, marker={"color": color}, hovertemplate=hover)
    )
    fig = _layout(go.Figure(trace), height=height)
    if horizontal:
        fig.update_yaxes(autorange="reversed", showgrid=False, type="category")
        fig.update_xaxes(rangemode="tozero")
    else:
        fig.update_xaxes(showgrid=False, type="category")
        fig.update_yaxes(rangemode="tozero")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def line(
    x: list[Any], y: list[float], *, value_format: str = ",.2f", hover_label: str = "", height: int = 260
) -> None:
    color = SERIES[_mode()]
    trace = go.Scatter(
        x=x,
        y=y,
        mode="lines+markers",
        line={"color": color, "width": 2},
        marker={"size": 8, "color": color},
        hovertemplate=f"%{{x}}<br>{hover_label}%{{y:{value_format}}}<extra></extra>",
    )
    fig = _layout(go.Figure(trace), height=height)
    fig.update_layout(hovermode="x")
    fig.update_xaxes(
        showgrid=False,
        type="category",
        showspikes=True,
        spikemode="across",
        spikethickness=1,
        spikedash="solid",
    )
    fig.update_yaxes(rangemode="tozero")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
