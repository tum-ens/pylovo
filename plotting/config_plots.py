import pandapower
import plotly.express as px
import seaborn as sns

ACCESS_TOKEN_PLOTLY = 'pk.eyJ1IjoibXVza2F0bnVzcyIsImEiOiJjbGdxYnVyY28wN3lhM2VvMnBtYWwxZGl6In0.y5gIcGJSpwb0X4wgQQVolA'
px.set_mapbox_access_token(ACCESS_TOKEN_PLOTLY)
pandapower.plotting.plotly.mapbox_plot.set_mapbox_token(ACCESS_TOKEN_PLOTLY)

TUMBlue = '#0065BD'
TUMGreen = '#A2AD00'
TUMOrange = '#E37222'
TUMIvory = '#DAD7CB'
TUMBlue4 = '#98C6EA'
TUMBlue2 = '#005293'
TUMGray2 = '#7F7F7F'

TUMPalette = [TUMBlue, TUMGreen, TUMOrange, TUMBlue4, TUMGray2, TUMBlue2]
TUMPalette1 = [TUMBlue, TUMGreen, TUMOrange, TUMIvory, TUMBlue4, TUMBlue2, TUMGray2]
TUMPalette2 = [TUMBlue, TUMGreen, TUMOrange, TUMBlue4, TUMBlue2, TUMGray2]
TUMPalette3 = [TUMBlue, TUMGreen, TUMOrange]

sns.set_palette(sns.color_palette(TUMPalette))

NODE_COLOR_TRAFO = TUMIvory
NODE_COLOR_CONSUMER = TUMBlue4
NODE_COLOR_CONNECTION_BUS = TUMGreen
