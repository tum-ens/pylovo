import plotly
import pandas as pd
import plotly.express as px
import plotly.io as io
io.renderers.default = "browser"
from maptool.config import * 

def aggregateHTMLFiles(filenames):
    #TODO: combine a given number of divs into single html file to be passed to the frontend
    return 0


def cap_pro_generate_plot(hdf_path, site_name, save_path=URBS_RESULT_PLOT_SAVE_PATH):
    hdf = pd.read_hdf(hdf_path, key='result/cap_pro')
    
    #----------------------------GENERATE PLOT HERE----------------------------#
    cap_pro = hdf.loc[:, site_name].groupby("pro").sum()
    fig = px.bar(cap_pro)
    #----------------------------GENERATE PLOT HERE----------------------------#
    
    #filename = site_name + "_cap_pro"
    filename = 'cap_pro'
    fig.write_html(save_path + filename + ".html", full_html=False, include_plotlyjs=False, div_id=site_name + filename)
    return(filename)

def e_pro_in_generate_plot(hdf_path, site_name, save_path=URBS_RESULT_PLOT_SAVE_PATH):
    hdf = pd.read_hdf(hdf_path, key='result/e_pro_in')

    #----------------------------GENERATE PLOT HERE----------------------------#
    rooftop_pv = hdf.loc[:,:, site_name, "Rooftop PV"].groupby("t").sum()
    Heat_dummy_space = hdf.loc[:,:, site_name, "Heat_dummy_space"].groupby("t").sum()
    Heat_dummy_water = hdf.loc[:,:, site_name, "Heat_dummy_water"].groupby("t").sum()

    fig = px.bar(y=[Heat_dummy_space, Heat_dummy_water, rooftop_pv])
    #----------------------------GENERATE PLOT HERE----------------------------#
    
    #filename = site_name + "_e_pro_in"
    filename = 'e_pro_in'
    fig.write_html(save_path + filename + ".html", full_html=False, include_plotlyjs=False, div_id=site_name + filename)
    return(filename)

def e_pro_out_generate_plot(hdf_path, site_name, save_path=URBS_RESULT_PLOT_SAVE_PATH):
    hdf = pd.read_hdf(hdf_path, key='result/e_pro_out')

    #----------------------------GENERATE PLOT HERE----------------------------#
    rooftop_pv = hdf.loc[:,:, site_name, "Rooftop PV"].groupby("t").sum()
    Heat_dummy_space = hdf.loc[:,:, site_name, "Heat_dummy_space"].groupby("t").sum()
    Heat_dummy_water = hdf.loc[:,:, site_name, "Heat_dummy_water"].groupby("t").sum()
    slack_heat = hdf.loc[:,:, site_name, "Slack_heat"].groupby("t").sum()

    fig = px.bar(y=[Heat_dummy_space, Heat_dummy_water, slack_heat, rooftop_pv])
    #----------------------------GENERATE PLOT HERE----------------------------#

    #filename = site_name + "_e_pro_out"
    filename = 'e_pro_out'
    fig.write_html(save_path + filename + ".html",full_html=False, include_plotlyjs=False, div_id=site_name + filename)
    return(filename)




def cap_sto_c_generate_plot(hdf_path, site_name, save_path=URBS_RESULT_PLOT_SAVE_PATH):
    hdf = pd.read_hdf(hdf_path, key='result/cap_sto_c')
    
    #----------------------------GENERATE PLOT HERE----------------------------#
    battery_private = hdf.loc[:,site_name, 'battery_private'].groupby(["com"]).sum()
    heat_storage = hdf.loc[:,site_name, 'heat_storage'].groupby(["com"]).sum()
    mobility_storage1 = hdf.loc[:,site_name, 'mobility_storage1'].groupby(["com"]).sum()

    fig = px.bar([battery_private,heat_storage,mobility_storage1])
    #----------------------------GENERATE PLOT HERE----------------------------#
    
    filename = site_name + "_cap_sto_c"
    fig.write_html(save_path + filename + ".html", full_html=False, include_plotlyjs=False, div_id=site_name + filename)
    return(filename)

def cap_sto_p_generate_plot(hdf_path, site_name, save_path=URBS_RESULT_PLOT_SAVE_PATH):
    hdf = pd.read_hdf(hdf_path, key='result/cap_sto_p')

    #----------------------------GENERATE PLOT HERE----------------------------#

    #----------------------------GENERATE PLOT HERE----------------------------#
    filename = site_name + "_cap_sto_p"
    return(filename)

def cap_tra_generate_plot(hdf_path, site_name, save_path=URBS_RESULT_PLOT_SAVE_PATH):
    hdf = pd.read_hdf(hdf_path, key='result/cap_tra')

    #----------------------------GENERATE PLOT HERE----------------------------#

    #----------------------------GENERATE PLOT HERE----------------------------#
    filename = site_name + "_cap_tra"
    return(filename)
